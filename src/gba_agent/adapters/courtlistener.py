"""CourtListener case-law adapter."""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from gba_agent.adapters.base import (
    AdapterContext,
    AdapterTask,
    FetchedDocument,
    NotSupported,
    invalid,
    iso_date,
    limit,
    string_parameter,
)
from gba_agent.clients.courtlistener_client import CourtListenerClient


class CourtListenerAdapter:
    site_id = "courtlistener"
    version = "1"

    def __init__(self, database: sessionmaker[Session]) -> None:
        self._database = database

    async def supports(self, task: AdapterTask) -> bool:
        return task.kind in {"search_opinions", "fetch_opinion", "crosscheck_citation"}

    async def run(self, task: AdapterTask, ctx: AdapterContext) -> list[FetchedDocument]:
        client = CourtListenerClient(ctx, self._database)
        if task.kind == "search_opinions":
            court = string_parameter(task, "court") if "court" in task.params else None
            return await client.search_opinions(
                string_parameter(task, "query"),
                maximum=limit(task),
                court=court,
            )
        if task.kind == "fetch_opinion":
            if "opinion_id" in task.params:
                opinion_id = task.params["opinion_id"]
                if type(opinion_id) is not int or "url" in task.params:
                    raise invalid("Use a single public opinion URL or opinion ID")
                return [await client.fetch_opinion(opinion_id=opinion_id)]
            return [await client.fetch_opinion(url=string_parameter(task, "url"))]
        if task.kind == "crosscheck_citation":
            document, result = await client.citation_document(
                string_parameter(task, "citation_string"),
                string_parameter(task, "case_name"),
                iso_date(task.params.get("decided_on")),
            )
            return [document.model_copy(update={"identity": result.model_dump(mode="json")})]
        raise NotSupported(task.kind)
