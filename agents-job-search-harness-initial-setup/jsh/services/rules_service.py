"""RuleRegistry/SearchProfileManager service: CRUD, versioning, profiles and simulation (spec §5)."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..db import Database, rule_snapshot
from ..models import AuditEvent, Job, JobModeEvaluation, JobSource, Rule, RuleVersion, SearchProfile, Setting
from ..rules import evaluate, parse_value, validate_rule
from ..rules.registry import VALUE_KIND
from .jobs import JobService

EDITABLE = ("name", "enabled", "scope", "field", "operator", "value", "value_type", "logic_group", "group_operator",
            "action", "weight", "severity", "unknown_handling", "explanation", "priority")


class RuleValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class RuleService:
    def __init__(self, db: Database, jobs: JobService):
        self.db = db
        self.jobs = jobs

    # ------------------------------------------------------------------ rules (active profile)
    def list(self, mode_id: str) -> list[Rule]:
        with self.db.session() as s:
            return self.jobs._rules(s, mode_id)

    def get(self, rule_id: str) -> Rule | None:
        with self.db.session() as s:
            return s.get(Rule, rule_id)

    @staticmethod
    def normalize(data: dict[str, Any]) -> dict[str, Any]:
        d = {k: data[k] for k in EDITABLE if k in data}
        if "operator" in d and "field" in d:
            d["value"] = parse_value(d["operator"], d["field"], d.get("value"))
            d["value_type"] = VALUE_KIND.get(d["operator"], "single")
        if "weight" in d:
            d["weight"] = float(d["weight"] or 0)
        if "priority" in d:
            d["priority"] = int(d["priority"] or 100)
        if "group_operator" in d:
            d["group_operator"] = (d["group_operator"] or "AND").upper()
        return d

    def _touch_profile(self, s, profile_id: str | None) -> None:
        prof = s.get(SearchProfile, profile_id) if profile_id else None
        if prof is not None:
            prof.version += 1

    def create(self, mode_id: str, data: dict[str, Any], actor: str = "user") -> Rule:
        d = self.normalize(data)
        errors = validate_rule(d)
        if errors:
            raise RuleValidationError(errors)
        with self.db.session() as s:
            pid = self.jobs.active_profile_id(s, mode_id)
            rule = Rule(mode_id=mode_id, profile_id=pid, **d)
            s.add(rule)
            s.flush()
            s.add(RuleVersion(rule_id=rule.id, version=1, change="created", snapshot=rule_snapshot(rule)))
            s.add(AuditEvent(entity_type="rule", entity_id=rule.id, action="rule_created", mode_id=mode_id,
                             actor=actor, details={"name": rule.name, "profile_id": pid}))
            self._touch_profile(s, pid)
            s.commit()
        self.jobs.reevaluate_mode(mode_id)
        return rule

    def update(self, rule_id: str, data: dict[str, Any], actor: str = "user") -> Rule:
        with self.db.session() as s:
            rule = s.get(Rule, rule_id)
            if rule is None:
                raise KeyError(rule_id)
            merged = {k: getattr(rule, k) for k in EDITABLE}
            merged.update(data)
            d = self.normalize(merged)
            errors = validate_rule(d)
            if errors:
                raise RuleValidationError(errors)
            if all(getattr(rule, k) == v for k, v in d.items()):
                return rule
            for k, v in d.items():
                setattr(rule, k, v)
            rule.version += 1
            s.add(RuleVersion(rule_id=rule.id, version=rule.version, change="updated", snapshot=rule_snapshot(rule)))
            s.add(AuditEvent(entity_type="rule", entity_id=rule.id, action="rule_updated", mode_id=rule.mode_id,
                             actor=actor, details={"name": rule.name, "version": rule.version}))
            self._touch_profile(s, rule.profile_id)
            s.commit()
            mode_id = rule.mode_id
        self.jobs.reevaluate_mode(mode_id)
        return rule

    def set_enabled(self, rule_id: str, enabled: bool) -> Rule:
        return self.update(rule_id, {"enabled": enabled})

    def delete(self, rule_id: str, actor: str = "user") -> None:
        with self.db.session() as s:
            rule = s.get(Rule, rule_id)
            if rule is None:
                return
            mode_id = rule.mode_id
            s.add(RuleVersion(rule_id=rule.id, version=rule.version + 1, change="deleted", snapshot=rule_snapshot(rule)))
            s.add(AuditEvent(entity_type="rule", entity_id=rule.id, action="rule_deleted", mode_id=mode_id,
                             actor=actor, details={"name": rule.name}))
            self._touch_profile(s, rule.profile_id)
            s.delete(rule)
            s.commit()
        self.jobs.reevaluate_mode(mode_id)

    def move(self, rule_id: str, delta: int) -> None:
        """Reorder by swapping priority with the neighbour (delta=-1 up, +1 down)."""
        with self.db.session() as s:
            rule = s.get(Rule, rule_id)
            mode_id = rule.mode_id
            rules = self.jobs._rules(s, mode_id)
            idx = [r.id for r in rules].index(rule_id)
            j = idx + delta
            if j < 0 or j >= len(rules):
                return
            for pos, r in enumerate(rules):
                r.priority = (pos + 1) * 10
            rules[idx].priority, rules[j].priority = rules[j].priority, rules[idx].priority
            s.commit()
        self.jobs.reevaluate_mode(mode_id)

    def versions(self, rule_id: str) -> list[RuleVersion]:
        with self.db.session() as s:
            return list(s.scalars(select(RuleVersion).where(RuleVersion.rule_id == rule_id)
                                  .order_by(RuleVersion.version.desc())))

    # ------------------------------------------------------------------ search profiles
    def profiles(self, mode_id: str) -> list[SearchProfile]:
        with self.db.session() as s:
            return list(s.scalars(select(SearchProfile).where(SearchProfile.mode_id == mode_id)
                                  .order_by(SearchProfile.created_at)))

    def active_profile(self, mode_id: str) -> SearchProfile | None:
        with self.db.session() as s:
            pid = self.jobs.active_profile_id(s, mode_id)
            return s.get(SearchProfile, pid) if pid else None

    def set_active_profile(self, mode_id: str, profile_id: str, actor: str = "user") -> None:
        with self.db.session() as s:
            prof = s.get(SearchProfile, profile_id)
            if prof is None or prof.mode_id != mode_id:
                raise KeyError(profile_id)
            key = f"active_profile:{mode_id}"
            row = s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=profile_id))
            else:
                row.value = profile_id
            s.add(AuditEvent(entity_type="profile", entity_id=profile_id, action="profile_activated",
                             mode_id=mode_id, actor=actor, details={"name": prof.name}))
            s.commit()
        self.jobs.reevaluate_mode(mode_id)

    def clone_profile(self, mode_id: str, name: str, actor: str = "user") -> SearchProfile:
        """Save the active profile's rules as a NEW profile and activate it (the original stays intact)."""
        name = name.strip()
        if not name:
            raise ValueError("El perfil necesita un nombre.")
        with self.db.session() as s:
            if s.scalar(select(SearchProfile).where(SearchProfile.mode_id == mode_id, SearchProfile.name == name)):
                raise ValueError(f"Ya existe un perfil llamado '{name}'.")
            source_id = self.jobs.active_profile_id(s, mode_id)
            source_rules = self.jobs._rules(s, mode_id)
            prof = SearchProfile(mode_id=mode_id, name=name, is_default=False)
            s.add(prof)
            s.flush()
            for r in source_rules:
                copy = Rule(mode_id=mode_id, profile_id=prof.id, **{k: getattr(r, k) for k in EDITABLE})
                s.add(copy)
                s.flush()
                s.add(RuleVersion(rule_id=copy.id, version=1, change="created",
                                  snapshot={**rule_snapshot(copy), "cloned_from": r.id}))
            s.add(AuditEvent(entity_type="profile", entity_id=prof.id, action="profile_created", mode_id=mode_id,
                             actor=actor, details={"name": name, "cloned_from": source_id}))
            s.commit()
            pid = prof.id
        self.set_active_profile(mode_id, pid, actor)
        return prof

    def rename_profile(self, profile_id: str, name: str, actor: str = "user") -> None:
        name = name.strip()
        if not name:
            raise ValueError("El perfil necesita un nombre.")
        with self.db.session() as s:
            prof = s.get(SearchProfile, profile_id)
            if s.scalar(select(SearchProfile).where(SearchProfile.mode_id == prof.mode_id,
                                                    SearchProfile.name == name, SearchProfile.id != profile_id)):
                raise ValueError(f"Ya existe un perfil llamado '{name}'.")
            old, prof.name = prof.name, name
            s.add(AuditEvent(entity_type="profile", entity_id=profile_id, action="profile_renamed",
                             mode_id=prof.mode_id, actor=actor, details={"from": old, "to": name}))
            s.commit()

    def delete_profile(self, profile_id: str, actor: str = "user") -> None:
        """Delete a non-active, non-default profile. Its rule history is kept in rule_versions."""
        with self.db.session() as s:
            prof = s.get(SearchProfile, profile_id)
            if prof is None:
                return
            if prof.is_default:
                raise ValueError("El perfil principal no se puede eliminar.")
            if self.jobs.active_profile_id(s, prof.mode_id) == profile_id:
                raise ValueError("Active otro perfil antes de eliminar este.")
            for r in s.scalars(select(Rule).where(Rule.profile_id == profile_id)):
                s.add(RuleVersion(rule_id=r.id, version=r.version + 1, change="deleted", snapshot=rule_snapshot(r)))
                s.delete(r)
            s.add(AuditEvent(entity_type="profile", entity_id=profile_id, action="profile_deleted",
                             mode_id=prof.mode_id, actor=actor, details={"name": prof.name}))
            s.delete(prof)
            s.commit()

    # ------------------------------------------------------------------ simulation
    def simulate(self, mode_id: str, draft: dict[str, Any] | None = None,
                 replace_rule_id: str | None = None) -> dict[str, Any]:
        """Preview the effect of a draft rule (added or replacing an existing one) without saving."""
        draft_norm = None
        if draft is not None:
            draft_norm = {**self.normalize(draft), "id": replace_rule_id or "__draft__", "enabled": True}
            errors = validate_rule(draft_norm)
            if errors:
                raise RuleValidationError(errors)
        with self.db.session() as s:
            current = self.jobs._rules(s, mode_id)
            proposed: list[Any] = [r for r in current if r.id != replace_rule_id]
            if draft_norm:
                proposed.append(draft_norm)
            jobs = list(s.scalars(select(Job)))
            evs = {e.job_id: e for e in s.scalars(select(JobModeEvaluation).where(JobModeEvaluation.mode_id == mode_id))}
            srcs = dict(s.execute(select(JobSource.job_id, JobSource.provider)).all())
        changes, counts = [], {}
        for job in jobs:
            facts = self.jobs.facts(job, evs.get(job.id), srcs.get(job.id, ""))
            before = evaluate(current, facts)
            after = evaluate(proposed, facts)
            counts[after.status] = counts.get(after.status, 0) + 1
            draft_outcome = next((o for o in after.outcomes if "__draft__" in o.rule_ids
                                  or (replace_rule_id and replace_rule_id in o.rule_ids)), None)
            changes.append({
                "job_id": job.id, "title": job.title, "company": job.company_name,
                "before": before.status, "after": after.status, "score_before": before.score,
                "score_after": after.score, "changed": before.status != after.status or before.score != after.score,
                "draft_result": draft_outcome.result if draft_outcome else "",
                "draft_effect": draft_outcome.effect if draft_outcome else "",
            })
        return {"total": len(jobs), "counts": counts, "jobs": changes,
                "changed": sum(1 for c in changes if c["changed"])}
