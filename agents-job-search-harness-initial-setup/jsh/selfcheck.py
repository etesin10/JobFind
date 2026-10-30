"""``JobSearchManager --self-check``: verifies the packaged app can parse résumés and use SQLite.

Runs entirely in a temporary folder (never touches user data) and exits 0 on success. Used by
packaging/smoke_test.py on Windows and macOS builds to prove that bundled dependencies (pypdf,
XML parser, SQLite, regex vocabulary) work inside the frozen executable.
"""
from __future__ import annotations

import io
import sys
import tempfile
import zipfile

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_LINES = ["SKILLS", "Python, SQL, Power BI", "EXPERIENCE", "Data Analyst | Example Corp | 2020 - Present",
          "Built reports with Python and SQL Server for the finance team."]


def _docx() -> bytes:
    body = "".join(f'<w:p><w:r><w:t>{t}</w:t></w:r></w:p>' for t in _LINES)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document xmlns:w="{_W}"><w:body>{body}'
                                        "</w:body></w:document>")
    return buf.getvalue()


def _pdf() -> bytes:
    content = "BT /F1 10 Tf 40 760 Td 13 TL " + " ".join(f"({t}) Tj T*" for t in _LINES) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()


def _session9_checks(app, candidate_id: str) -> list[tuple[str, bool]]:
    """JD analysis, review package, tailoring → .docx (python-docx), MCP tool registry and REST API."""
    import asyncio

    out = []
    app.candidate.set_item_status([i.id for i in app.candidate.items(candidate_id)], "approved")
    app.candidate.set_active_candidate("CANADA_VISA", candidate_id)
    job_id = app.jobs.rows("CANADA_VISA")[0]["job_id"]
    doc = app.jd.document(job_id, "CANADA_VISA")
    out.append(("jd-analysis", "Python" in {k["name"] for k in doc["jd"]["keywords"]}))
    pkg = app.reviews.build_package("CANADA_VISA", [job_id])
    out.append(("review-package", bool(pkg["response_schema"]) and pkg["jobs"][0]["jd_hash"] == doc["jd"]["hash"]))
    run = app.tailoring.deterministic_proposal(job_id, "CANADA_VISA")
    vid = app.tailoring.approve(run["run_id"], [c["change_id"] for c in app.tailoring.get_run(run["run_id"])["changes"]])
    rel = app.tailoring.generate_docx(vid)
    from docx import Document
    text = "\n".join(p.text for p in Document(app.paths.home / rel).paragraphs)
    out.append(("docx", "Python" in text))
    from .mcp_server import build_server

    async def tools() -> set[str]:
        return {t.name for t in await build_server(app).list_tools()}
    out.append(("mcp", {"search_jobs", "submit_job_review"} <= asyncio.run(tools())))
    from fastapi.testclient import TestClient

    from .api import create_api, ensure_tokens
    client = TestClient(create_api(app), base_url="http://127.0.0.1")
    r = client.get("/status", headers={"Authorization": f"Bearer {ensure_tokens(app)['read']}"})
    out.append(("rest-api", r.status_code == 200 and r.json()["jobs"] == 1))
    return out


def run() -> int:
    from .candidate.extraction import extract
    from .candidate.parsing import parse_resume
    from .paths import get_paths
    from .services import AppServices, JobInput

    checks = []
    for name, data in (("cv.docx", _docx()), ("cv.pdf", _pdf()), ("cv.txt", "\n".join(_LINES).encode())):
        ex = extract(parse_resume(data, name).lines)
        found = {s.name for s in ex.skills}
        ok = {"Python", "SQL", "Power BI"} <= found and any(i.kind == "position" for i in ex.items)
        checks.append((name, ok))
    with tempfile.TemporaryDirectory(prefix="jsh_selfcheck_") as tmp:
        app = AppServices(get_paths(tmp))
        try:
            cand = app.candidate.create_candidate("self-check")
            app.candidate.import_file(cand.id, "cv.pdf", _pdf())
            app.candidate.set_skill_status([s.id for s in app.candidate.skills(cand.id)], "approved")
            app.jobs.upsert_job(JobInput(title="T", canonical_url="https://selfcheck.invalid/1",
                                         description="Python and SQL required."))
            pct = app.jobs.rows("CANADA_VISA")[0]["skills_match"]
            checks.append(("sqlite+matching", pct == 100))
            checks.extend(_session9_checks(app, cand.id))
        finally:
            app.close()
    for name, ok in checks:
        print(f"{'OK  ' if ok else 'FAIL'} {name}")
    success = all(ok for _, ok in checks)
    print("SELF-CHECK OK" if success else "SELF-CHECK FAILED")
    sys.stdout.flush()
    return 0 if success else 1
