"""JD ↔ approved-skills matching (deterministic, explainable).

score = 100 · Σ weight(matched) / (Σ weight(matched) + Σ weight(missing))
- matched: skill requirements found in the JD that the candidate APPROVED (and kept active).
- missing: vocabulary skills found in the JD that are NOT approved. Each is tagged as
  "proposed" (pending approval), "rejected" (the user said no) or "absent" (never in the CV).
- Broad business ("domain") skills weigh 0.5 so incidental mentions do not dominate.
- No skills recognised in the JD, or no approved skills → None (unknown), never 0 or 100.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .vocabulary import BY_KEY, KIND_WEIGHT, find_vocab, fold, phrase_regex, snippet

MAX_LIST = 40
MIN_EVIDENCE_WEIGHT = 2.0  # recognised non-language skill weight in the JD needed to compute a percentage


@dataclass(frozen=True)
class ApprovedSkill:
    id: str
    name: str
    vocab_key: str
    aliases: tuple[str, ...]
    weight: float
    evidence: str = ""
    notes: str = ""


@dataclass
class SkillMatcher:
    candidate_id: str
    candidate_name: str
    candidate_version: int
    approved: list[ApprovedSkill]
    proposed_keys: set[str] = field(default_factory=set)  # vocab keys / names pending approval
    rejected_keys: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._by_vocab = {s.vocab_key: s for s in self.approved if s.vocab_key}
        # Custom skills (not in the vocabulary) and user aliases are matched with their own regex.
        self._custom = []
        for s in self.approved:
            phrases = list(s.aliases) + ([] if s.vocab_key else [s.name])
            rx = phrase_regex(phrases)
            if rx is not None:
                self._custom.append((s, rx))

    def match(self, jd_text: str) -> tuple[float | None, dict[str, Any] | None]:
        if not jd_text or not jd_text.strip() or not self.approved:
            return None, None
        folded = fold(jd_text)
        matched: dict[str, dict[str, Any]] = {}
        covered: list[tuple[int, int]] = []
        for skill, rx in self._custom:
            m = rx.search(folded)
            if m:
                covered.append((m.start(), m.end()))
                matched.setdefault(skill.id, self._entry(skill, jd_text, m.start(), m.end()))
        missing: list[dict[str, Any]] = []
        mw = 0.0
        miss_w = 0.0
        for key, start, end in find_vocab(jd_text):
            entry = BY_KEY[key]
            kind_w = KIND_WEIGHT.get(entry.kind, 1.0)
            skill = self._by_vocab.get(key)
            if skill is not None:
                matched.setdefault(skill.id, self._entry(skill, jd_text, start, end))
                continue
            if any(a < end and start < b for a, b in covered):
                continue  # the same JD words already matched one of the user's own skills/aliases
            status = "proposed" if key in self.proposed_keys else ("rejected" if key in self.rejected_keys
                                                                   else "absent")
            missing.append({"name": key, "category": entry.category, "kind": entry.kind, "status": status,
                            "jd": snippet(jd_text, start, end, 60), "weight": kind_w})
            miss_w += kind_w
        for m in matched.values():
            mw += m["weight"]
        if mw + miss_w == 0:
            return None, None
        # Evidence threshold: spoken languages and a couple of broad business words are not enough to score a
        # job (real case: an automation role scored 60% on "English" + "documentation"). Below it → unknown.
        evidence_w = sum(KIND_WEIGHT.get(d["kind"], 1.0) for d in [*matched.values(), *missing]
                         if d["kind"] != "language")
        enough = evidence_w >= MIN_EVIDENCE_WEIGHT
        pct = round(100.0 * mw / (mw + miss_w), 1) if enough else None
        detail = {
            "candidate_id": self.candidate_id, "candidate_name": self.candidate_name,
            "candidate_version": self.candidate_version, "pct": pct, "insufficient": not enough,
            "matched": sorted(matched.values(), key=lambda d: d["name"].lower())[:MAX_LIST],
            "missing": sorted(missing, key=lambda d: (d["status"] != "proposed", -d["weight"], d["name"]))[:MAX_LIST],
        }
        return pct, detail

    @staticmethod
    def _entry(skill: ApprovedSkill, jd_text: str, start: int, end: int) -> dict[str, Any]:
        kind = BY_KEY[skill.vocab_key].kind if skill.vocab_key in BY_KEY else "hard"
        return {"name": skill.name, "skill_id": skill.id, "jd": snippet(jd_text, start, end, 60),
                "cv": skill.evidence, "notes": skill.notes, "kind": kind,
                "weight": round(skill.weight * KIND_WEIGHT.get(kind, 1.0), 3)}
