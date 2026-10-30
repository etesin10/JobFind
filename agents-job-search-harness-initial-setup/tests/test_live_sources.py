"""LIVE source checks (real network). Skipped unless JSH_LIVE_TESTS=1.

    set JSH_LIVE_TESTS=1 && .venv\\Scripts\\python -m pytest tests\\test_live_sources.py -q

These verify that the public sources still answer in the expected format. They make a handful of polite requests.
"""
import os

import pytest

from jsh.connectors import CONNECTORS, HttpClient, SourceQuery

pytestmark = pytest.mark.skipif(os.environ.get("JSH_LIVE_TESTS") != "1", reason="live network test (JSH_LIVE_TESTS=1)")


@pytest.mark.parametrize("provider,query", [
    ("amazon_jobs", SourceQuery(keywords=["data engineer"], countries=["CA"], max_results=20)),
    ("greenhouse", SourceQuery(identifier="gitlab", keywords=["engineer"], max_results=5)),
    ("lever", SourceQuery(identifier="palantir", max_results=5)),
    ("ashby", SourceQuery(identifier="ashby", max_results=5)),
])
def test_live_source_format(provider, query):
    conn = CONNECTORS[provider]
    with HttpClient(conn.hosts, min_interval=conn.min_interval) as http:
        res = conn.fetch(query, http)
    assert res.jobs, f"{provider} returned no jobs"
    job = res.jobs[0]
    assert job.title and job.source_job_id and job.canonical_url.startswith("https://")
    assert len(job.description) > 100
