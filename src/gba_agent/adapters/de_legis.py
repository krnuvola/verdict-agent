"""Delaware General Assembly bill detail and published version links."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from gba_agent.adapters.base import (
    AdapterContext,
    AdapterTask,
    AdapterUnavailable,
    FetchedDocument,
    Links,
    NotSupported,
    invalid,
)

_VERSION = {
    "original": "GenerateHtmlDocument",
    "engrossed": "GenerateHtmlDocumentEngrossment",
    "session_law": "GenerateHtmlDocumentSessionLaw",
}


class DelawareLegislatureAdapter:
    site_id = "de_legislature"
    version = "1"

    async def supports(self, task: AdapterTask) -> bool:
        return task.kind == "fetch_bill"

    async def run(self, task: AdapterTask, ctx: AdapterContext) -> list[FetchedDocument]:
        if not await self.supports(task):
            raise NotSupported(task.kind)
        bill, assembly = (str(task.params.get(key, "")) for key in ("bill_id", "general_assembly"))
        if not re.fullmatch(r"[1-9]\d{0,8}", bill) or not re.fullmatch(r"[1-9]\d{1,2}", assembly):
            raise invalid("Use the numeric BillDetail identifier and General Assembly number")
        version = task.params.get("version", "original")
        if not isinstance(version, str) or version not in _VERSION:
            raise invalid("Bill version must be original, engrossed or session_law")
        detail = await ctx.fetch(self.site_id, f"https://legis.delaware.gov/BillDetail/{bill}")
        page = Links(detail.raw)
        if not re.search(
            rf"\b{assembly}(?:st|nd|rd|th)\s+General Assembly\b", " ".join(page.text), re.I
        ):
            raise AdapterUnavailable("BillDetail did not match the requested General Assembly")
        expected_path = "/json/BillDetail/" + _VERSION[version]
        candidates = {
            target
            for href, label in page.links
            if label.casefold() == "view html"
            and urlsplit(target := urljoin(detail.url, href)).path.casefold()
            == expected_path.casefold()
        }
        if len(candidates) != 1:
            raise AdapterUnavailable("The requested bill version has no unique published HTML link")
        document = await ctx.fetch(
            self.site_id,
            candidates.pop(),
            expected_doc_type="session_law" if version == "session_law" else "bill",
            version_label=f"{assembly} General Assembly BillDetail {bill} {version}",
        )
        return [
            document.model_copy(
                update={
                    "identity": {"bill_id": bill, "general_assembly": assembly, "version": version},
                }
            )
        ]
