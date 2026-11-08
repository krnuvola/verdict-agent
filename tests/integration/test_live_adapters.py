"""Optional read-only source probes with caller-supplied identifiers."""

import json
import os

import pytest

from gba_agent.access.fetcher import Fetcher
from gba_agent.adapters.base import AdapterContext, AdapterRegistry, AdapterTask
from gba_agent.adapters.courtlistener import CourtListenerAdapter
from gba_agent.config import get_settings
from tests.sitesim.fetcher import RUN_ID, FetchFixture
from tests.sitesim.fetcher import site as site


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry_id", "kind"),
    [
        ("sec_edgar", "fetch_url"),
        ("de_code_online", "fetch_statute"),
        ("de_legislature", "fetch_bill"),
        ("de_courts", "fetch_opinion"),
        ("courtlistener", "fetch_opinion"),
    ],
)
async def test_live_public_document(site: FetchFixture, entry_id: str, kind: str) -> None:
    variable = "GBA_LIVE_" + entry_id.upper() + "_PARAMS"
    raw = os.environ.get(variable)
    if raw is None:
        pytest.skip(f"Set {variable} to JSON parameters for a public document")
    settings = get_settings()
    context = AdapterContext(Fetcher(settings, site.database, site.blobs), RUN_ID)
    registry = AdapterRegistry.defaults(CourtListenerAdapter(site.database))
    request = AdapterTask.model_validate({"kind": kind, "params": json.loads(raw)})
    documents = await registry.run(entry_id, request, context)
    assert documents and all(document.http_status == 200 for document in documents)
    assert all(not row.admitted for row in site.sources())
