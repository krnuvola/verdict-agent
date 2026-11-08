"""Delaware Code Online. Returns the published HTML containing the requested section."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
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


class DelawareCodeAdapter:
    site_id = "de_code_online"
    version = "1"

    def __init__(
        self,
        chapter_pages: Mapping[tuple[str, str], tuple[str, ...]] | None = None,
    ) -> None:
        self._chapters = dict(chapter_pages or {})

    async def supports(self, task: AdapterTask) -> bool:
        return task.kind == "fetch_statute"

    async def run(self, task: AdapterTask, ctx: AdapterContext) -> list[FetchedDocument]:
        if not await self.supports(task):
            raise NotSupported(task.kind)
        title, chapter, section = (
            str(task.params.get(name, "")) for name in ("title", "chapter", "section")
        )
        if not re.fullmatch(r"[1-9][0-9]{0,2}[A-Za-z]?", title):
            raise invalid("Invalid Delaware Code title")
        if not re.fullmatch(r"[1-9][0-9]{0,2}[A-Za-z]?", chapter):
            raise invalid("Invalid Delaware Code chapter")
        if not re.fullmatch(r"[1-9][0-9]{0,4}[A-Za-z]{0,2}", section):
            raise invalid("Invalid Delaware Code section")
        chapter_key = re.fullmatch(r"(\d+)([A-Za-z]?)", chapter)
        assert chapter_key is not None
        prefix = f"/title{title.lower()}/c{int(chapter_key[1]):03}{chapter_key[2].lower()}/"
        base = "https://delcode.delaware.gov"
        explicit = task.params.get("url")
        subchapter = task.params.get("subchapter")
        if explicit is not None and not isinstance(explicit, str):
            raise invalid("Statute URL must be text")
        if subchapter is not None and not re.fullmatch(r"[0-9]{1,3}[A-Za-z]?", str(subchapter)):
            raise invalid("Invalid Delaware Code subchapter")
        if explicit:
            candidates = [explicit]
        elif subchapter is not None:
            number = re.fullmatch(r"(\d+)([A-Za-z]?)", str(subchapter))
            assert number is not None
            candidates = [f"{base}{prefix}sc{int(number[1]):02}{number[2].lower()}/index.html"]
        else:
            configured = ctx.fetcher.site_metadata(self.site_id).get("chapter_pages", {})
            pages = configured.get(f"{title}/{chapter}") if isinstance(configured, dict) else None
            if pages is not None and (
                not isinstance(pages, list) or not all(isinstance(page, str) for page in pages)
            ):
                raise AdapterUnavailable("Delaware Code chapter routing is invalid")
            candidates = list(
                self._chapters.get(
                    (title, chapter), tuple(pages) if pages is not None else (base + prefix,)
                )
            )
        seen = set()
        pattern = re.compile(rf"^§\s*{re.escape(section)}(?:\s|\.|$)", re.I)
        version = "retrieved " + datetime.now(UTC).date().isoformat()
        for url in candidates:
            path = urlsplit(url).path
            if url in seen or not path.lower().startswith(prefix):
                continue
            seen.add(url)
            if len(seen) > 30:
                break
            document = await ctx.fetch(
                self.site_id, url, expected_doc_type="statute", version_label=version
            )
            if document.media_type != "text/html":
                raise AdapterUnavailable("Delaware Code did not return published HTML")
            index = Links(document.raw)
            if any(
                pattern.match(label) for href, label in index.links if href.startswith("#")
            ) or any(pattern.match(text) for text in index.text):
                return [
                    document.model_copy(
                        update={
                            "identity": {"title": title, "chapter": chapter, "section": section},
                        }
                    )
                ]
            for href, _ in index.links:
                target = urljoin(document.url, href).split("#", 1)[0]
                path = urlsplit(target).path.lower()
                if (
                    path.startswith(prefix)
                    and re.fullmatch(r"sc[0-9]+[a-z]?/(?:index\.html)?", path[len(prefix) :])
                    and target not in seen
                    and target not in candidates
                ):
                    candidates.append(target)
        raise AdapterUnavailable("Requested section was not found in the published chapter pages")
