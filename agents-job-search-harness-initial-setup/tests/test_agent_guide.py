"""AGENT_GUIDE.md must describe the real interfaces: every MCP tool and REST route it lists exists (and vice versa),
and its JSON/Python examples are accepted by the app."""
import json
import re
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from mcp import Client

from jsh.api import create_api, ensure_tokens
from jsh.mcp_server import build_server
from jsh.modes import CANADA, DISCARD_REASONS, USER_DECISIONS
from jsh.services.reviews import ReviewResult
from jsh.services.tailoring import TailoringProposal

GUIDE = (Path(__file__).resolve().parents[1] / "AGENT_GUIDE.md").read_text(encoding="utf-8")


def _section(title: str) -> str:
    return GUIDE.split(title, 1)[1].split("\n## ", 1)[0]


async def test_mcp_tool_table_matches_the_server(app):
    documented = set(re.findall(r"^\| `(\w+)\(", _section("## 4. MCP tools"), re.M))
    async with Client(build_server(app, "doc")) as c:
        real = {t.name for t in (await c.list_tools()).tools}
    assert documented == real
    assert f"## 4. MCP tools ({len(real)})" in GUIDE


def test_rest_table_matches_the_api(app):
    paths = set()
    for line in _section("## 5. REST endpoints").splitlines():
        for m in re.finditer(r"(GET|POST) (/[\w{}/-]+)", line):
            paths.add((m.group(1), m.group(2)))
    tokens = ensure_tokens(app)
    spec = TestClient(create_api(app), base_url="http://127.0.0.1").get("/openapi.json").json()
    real = {(meth.upper(), path) for path, ops in spec["paths"].items() for meth in ops}
    assert paths <= real, paths - real
    assert real <= paths, real - paths
    assert tokens["read"].startswith("jshr_") and tokens["write"].startswith("jshw_")


def test_protocol_examples_validate():
    blocks = re.findall(r"```json\n(.*?)```", GUIDE, re.S)
    review = next(json.loads(b) for b in blocks if '"jsh-review/1"' in b)
    tailoring = next(json.loads(b) for b in blocks if '"jsh-tailoring/1"' in b)
    ReviewResult.model_validate(review)
    TailoringProposal.model_validate(tailoring)


def test_glossary_values_are_real():
    glossary = _section("## 9. Field glossary")
    assert all(f"`{d}`" in glossary for d in USER_DECISIONS)
    assert all(f"`{r}`" in glossary for r in DISCARD_REASONS)


def test_inbox_python_example_works(app):
    code = re.search(r"```python\n(.*?)```", GUIDE, re.S).group(1)
    code = code.replace(r'r"C:\JobSearchManager\data\jobsearch.db"', repr(str(app.paths.db_file)))
    exec(compile(code, "AGENT_GUIDE.md", "exec"), {})
    app.sync.tick(force=True)
    con = sqlite3.connect(app.paths.db_file)
    assert con.execute("SELECT status, result FROM job_inbox WHERE source='my-agent'").fetchone() == (
        "imported", "created")
    assert app.jobs.rows(CANADA, "all")[0]["agent_score"] == 74
