"""S3 — a historical matter cannot see a later enactment."""

import pytest

from gba_agent.clients.graph_client import GraphClient
from gba_agent.clients.http import UpstreamServiceError
from gba_agent.guards.temporal_guard import TemporalGuard
from tests.mocks.temporal import HOLDING, PROVISION_B, known, matter
from tests.mocks.temporal import graph as graph


@pytest.mark.asyncio
async def test_s3_point_in_time_visibility(graph: GraphClient) -> None:
    fixture = matter(
        "2024-12-01",
        signing_date=known("2023-05-01"),
        litigation_commenced_on=known("2023-11-01"),
    )
    with pytest.raises(UpstreamServiceError) as hidden:
        await graph.get_node(PROVISION_B, fixture.as_of)
    assert (hidden.value.status_code, hidden.value.code) == (404, "NOT_KNOWN_AS_OF")
    guard = TemporalGuard(graph)
    provision = await guard.check(PROVISION_B, fixture)
    assert (provision.verdict, provision.reason_code) == ("block", "TEMPORAL_NOT_IN_FORCE")
    assert provision.evidence["graph_code"] == "NOT_KNOWN_AS_OF"
    holding = await guard.check(HOLDING, fixture)
    assert (holding.verdict, holding.reason_code) == ("pass", "TEMPORAL_OK")
