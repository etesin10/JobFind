"""Job source connectors (Phase 3). Registry of providers available in the UI."""
from __future__ import annotations

from .amazon import AmazonConnector
from .ats import AshbyConnector, GreenhouseConnector, LeverConnector
from .base import ConnectorError, FetchResult, HttpClient, SourceQuery

CONNECTORS = {c.provider: c for c in (AmazonConnector(), GreenhouseConnector(), LeverConnector(), AshbyConnector())}

__all__ = ["CONNECTORS", "ConnectorError", "FetchResult", "HttpClient", "SourceQuery"]
