"""Insights: skill-gap report across saved/active jobs (what the JDs ask for that the approved skills don't cover).

Deterministic and local: it aggregates the per-job match computed by the rule engine (``skills_detail``) and weighs
each missing skill by where the JD mentions it (required ×3, preferred ×2, elsewhere ×1).
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select

from ..candidate.vocabulary import BY_KEY
from ..jd import analyze_jd, importance_of
from ..models import Job, JobModeEvaluation
from ..modes import MODE_DEFS

SCOPES = ("activos", "saved", "all")
IMPORTANCE_WEIGHT = {"required": 3, "preferred": 2, "mentioned": 1}
RECOMMENDATION = {
    "absent": "No está en tu perfil: si tienes experiencia real, agrégala en Skills; si no, es candidata a aprender.",
    "proposed": "Ya está propuesta (sin aprobar): revísala y apruébala si es verdadera.",
    "rejected": "La rechazaste antes: reconsidera solo si ahora tienes evidencia.",
}


class InsightService:
    def __init__(self, app):
        self.app = app
        self.db = app.db

    def _job_ids(self, mode_id: str, scope: str) -> list[str]:
        if scope not in SCOPES:
            raise ValueError(f"Alcance inválido: {scope} (opciones: {', '.join(SCOPES)})")
        rows = self.app.jobs.rows(mode_id, "activos" if scope == "activos" else "all")
        if scope == "saved":
            rows = [r for r in rows if r["user_decision"] in ("saved", "maybe")
                    or r["application_status"] not in ("", "discovered")]
        return [r["job_id"] for r in rows if r["user_decision"] != "discarded"]

    def skill_gaps(self, mode_id: str, scope: str = "activos", limit: int = 30) -> dict[str, Any]:
        if mode_id not in MODE_DEFS:
            raise ValueError(f"Modo inválido: {mode_id}")
        ids = set(self._job_ids(mode_id, scope))
        with self.db.session() as s:
            data = s.execute(select(Job.id, Job.title, Job.company_name, Job.description,
                                    JobModeEvaluation.skills_detail)
                             .join(JobModeEvaluation, JobModeEvaluation.job_id == Job.id)
                             .where(JobModeEvaluation.mode_id == mode_id)).all()
        gaps: dict[str, dict[str, Any]] = {}
        strengths: Counter[str] = Counter()
        analysed = with_match = 0
        for jid, title, company, desc, detail in data:
            if jid not in ids:
                continue
            analysed += 1
            if not detail:
                continue
            with_match += 1
            for m in detail.get("matched", []):
                strengths[m["name"]] += 1
            missing = detail.get("missing", [])
            if not missing:
                continue
            jd = analyze_jd(desc or "")
            for m in missing:
                name = m["name"]
                entry = BY_KEY.get(name)
                imp = importance_of(jd, [name, *(entry.aliases if entry else ())])
                g = gaps.setdefault(name, {"name": name, "category": m.get("category", ""),
                                           "kind": m.get("kind", ""), "status": m.get("status", "absent"),
                                           "jobs": 0, "required": 0, "preferred": 0, "mentioned": 0, "score": 0,
                                           "examples": []})
                g["jobs"] += 1
                g[imp] += 1
                g["score"] += IMPORTANCE_WEIGHT[imp]
                if len(g["examples"]) < 5:
                    g["examples"].append({"job_id": jid, "title": title, "company_name": company,
                                          "importance": imp, "jd": m.get("jd", "")})
        current = self._current_status(mode_id)
        for g in gaps.values():
            g["status"] = current.get(g["name"].casefold(), g["status"])
            g["recommendation"] = RECOMMENDATION.get(g["status"], RECOMMENDATION["absent"])
            g["share_pct"] = round(100.0 * g["jobs"] / with_match, 1) if with_match else 0.0
        ranked = sorted(gaps.values(), key=lambda g: (-g["score"], -g["jobs"], g["name"].lower()))
        return {"mode_id": mode_id, "scope": scope, "jobs_analysed": analysed, "jobs_with_match": with_match,
                "gaps": ranked[:max(1, limit)],
                "strengths": [{"name": n, "jobs": c} for n, c in strengths.most_common(15)],
                "note": ("Basado en el vocabulario de skills y en los skills APROBADOS del perfil activo del modo. "
                         "Las vacantes sin JD o con poca evidencia no cuentan.")}

    def _current_status(self, mode_id: str) -> dict[str, str]:
        cand = self.app.candidate.active_candidate(mode_id)
        if cand is None:
            return {}
        out: dict[str, str] = {}
        for sk in self.app.candidate.skills(cand.id):
            for key in {sk.name, sk.vocab_key or ""} - {""}:
                if sk.status == "approved" and not sk.active:
                    out[key.casefold()] = "rejected"  # deactivated: treat like not available
                elif sk.status != "approved":
                    out[key.casefold()] = sk.status
        return out

    def proposed_skill_id(self, mode_id: str, name: str) -> str | None:
        cand = self.app.candidate.active_candidate(mode_id)
        if cand is None:
            return None
        key = name.casefold()
        for sk in self.app.candidate.skills(cand.id, "proposed"):
            if sk.name.casefold() == key or (sk.vocab_key or "").casefold() == key:
                return sk.id
        return None

    def markdown(self, report: dict[str, Any]) -> str:
        mode = MODE_DEFS[report["mode_id"]].name
        lines = [f"# Brechas de skills — {mode} ({report['scope']})", "",
                 f"Vacantes analizadas: {report['jobs_analysed']} (con coincidencia calculada: "
                 f"{report['jobs_with_match']})", "",
                 "| # | Skill | Vacantes | Requerido | Deseable | Mencionado | Estado |",
                 "|---|---|---|---|---|---|---|"]
        for i, g in enumerate(report["gaps"], 1):
            lines.append(f"| {i} | {g['name']} | {g['jobs']} ({g['share_pct']}%) | {g['required']} | "
                         f"{g['preferred']} | {g['mentioned']} | {g['status']} |")
        if report["strengths"]:
            lines += ["", "## Fortalezas (skills aprobados más pedidos)", ""]
            lines += [f"- {s['name']}: {s['jobs']} vacantes" for s in report["strengths"]]
        lines += ["", f"_{report['note']}_"]
        return "\n".join(lines) + "\n"
