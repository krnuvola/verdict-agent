"""DOM-first Chromium perception. Page values never become model inputs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import CDPSession, Page
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, Field

from gba_agent.access.block_detector import DOMSignals

# Reused for observation and immediate execution-time target verification.
DESCRIBE = r"""(element) => {
    const clean = x => (x || '').trim().replace(/\s+/g, ' ').slice(0, 160);
    const path = e => {
        const parts = [];
        for (; e; e = e.parentElement) {
            let position = 1;
            for (let s = e.previousElementSibling; s; s = s.previousElementSibling) position++;
            parts.unshift(e.localName + ':nth-child(' + position + ')');
        }
        return parts.join(' > ');
    };
    const e = element, f = e.form || e.closest('form'), r = e.getBoundingClientRect();
    const tag = e.localName, type = e.type || '';
    const submit = f && (tag === 'button' || ['submit', 'image'].includes(type));
    const labels = Array.from(e.labels || [], l => l.innerText).join(' ');
    const referenced = (e.getAttribute('aria-labelledby') || '').split(/\s+/)
        .map(id => document.getElementById(id)?.textContent || '').join(' ').trim();
    const buttonLabel = ['submit','button','reset','image'].includes(type)
        ? (e.getAttribute('value') || e.getAttribute('alt')) : '';
    const fullName = e.getAttribute('aria-label') || referenced || labels
        || e.getAttribute('title') || e.innerText || buttonLabel || e.getAttribute('name') || '';
    const name = clean(fullName);
    return {selector:path(e), tag, input_type:type || null, name, label:clean(labels),
        text:clean(e.innerText), autocomplete:e.getAttribute('autocomplete'),
        ambiguous_label:Math.max(fullName.length, labels.length, (e.innerText || '').length) > 160,
        role:e.getAttribute('role') || ({a:'link', button:'button', select:'combobox',
          textarea:'textbox', input:'textbox'})[tag] || null,
        href:tag === 'a' && e.hasAttribute('href') ? e.href : null,
        opens_tab:tag === 'a' && (e.target || document.querySelector('base[target]')?.target)
          ?.toLowerCase() === '_blank',
        form_action:f ? (submit && e.hasAttribute('formaction') ? e.formAction : f.action) : null,
        form_method:f ? (submit && e.hasAttribute('formmethod') ? e.formMethod : f.method) : null,
        visible:e.isConnected && e.checkVisibility({checkVisibilityCSS:true, checkOpacity:true})
          && r.width > 0 && r.height > 0,
        enabled:!e.matches(':disabled') && e.getAttribute('aria-disabled') !== 'true',
        box:{x:r.x,y:r.y,width:r.width,height:r.height}};
}"""

_COLLECT = r"""describe => {
    const selector = 'a[href],button,input:not([type=hidden]),select,textarea,summary,'
        + '[contenteditable=true],[role=button],[role=link],[role=combobox],[tabindex]';
    const elements = [];
    for (const element of document.querySelectorAll(selector)) {
        const item = describe(element);
        if (item.visible && item.enabled) elements.push(item);
        if (elements.length === 200) break;
    }
    const state = Array.from(document.querySelectorAll('select, input:not([type=password])'), e =>
        [e.selectedIndex ?? null, e.checked ?? null,
         e.matches('input[type=text],input:not([type]),input[type=search]') 
            ? e.value.length : null]);
    return {elements, state, text:(document.body?.innerText || '').slice(0,6000),
        frames:Array.from(document.querySelectorAll('iframe[src]'), f => f.src).slice(0,200),
        password:!!document.querySelector('input[type=password]'),
        viewer:!!document.querySelector('canvas,embed,object,iframe[src]')};
}"""

_SUBMITTER = """e => {
    if (!e.form || e.matches('button,input[type=submit],input[type=image]')) return e;
    const selector = 'button,input[type=submit],input[type=image]';
    return Array.from(e.ownerDocument.querySelectorAll(selector))
        .find(x => x.form === e.form && ['submit','image'].includes(x.type)) || e;
}"""


def public_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))


@dataclass(frozen=True)
class Element:
    index: int
    data: dict[str, Any] = field(repr=False)

    def model_fields(self) -> dict[str, Any]:
        return {
            key: self.data.get(key)
            for key in (
                "role",
                "name",
                "tag",
                "input_type",
                "href",
                "form_method",
                "form_action",
                "text",
                "selector",
            )
        } | {"index": self.index}


class Observation(BaseModel):
    observation_id: int
    url: str
    title: str = Field(repr=False)
    elements: list[dict[str, Any]] = Field(repr=False)
    text_excerpt: str = Field(repr=False)
    ax_digest: str
    screenshot_png: bytes | None = Field(default=None, repr=False)
    network: list[dict[str, Any]]
    downloads: list[dict[str, Any]]
    dialogs: list[str]
    tabs: list[str]


@dataclass(frozen=True)
class Perception:
    observation: Observation
    elements: list[Element] = field(repr=False)
    dom: DOMSignals = field(repr=False)
    embedded_viewer: bool = False


async def _isolated(cdp: CDPSession, expression: str) -> Any:
    frame = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]
    world = await cdp.send(
        "Page.createIsolatedWorld",
        {
            "frameId": frame["id"],
            "worldName": "gba-perception",
        },
    )
    evaluated = await cdp.send(
        "Runtime.evaluate",
        {
            "expression": expression,
            "contextId": world["executionContextId"],
            "returnByValue": True,
        },
    )
    if "exceptionDetails" in evaluated:
        raise PlaywrightError("Chromium DOM inspection failed")
    return evaluated["result"].get("value")


async def describe_target(
    page: Page,
    selector: str,
    enter: bool,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    expression = f"""(() => {{
        const node = document.querySelector({json.dumps(selector)});
        if (!node) return null;
        const describe = {DESCRIBE};
        const submitter = {str(enter).lower()} ? ({_SUBMITTER})(node) : node;
        return [describe(node), describe(submitter)];
    }})()"""
    cdp = await page.context.new_cdp_session(page)
    try:
        data = await _isolated(cdp, expression)
        return (data[0], data[1]) if data is not None else None
    finally:
        await cdp.detach()


def _backend_selectors(node: dict[str, Any], parent: str, mapping: dict[int, str]) -> None:
    position = 0
    for child in node.get("children", []):
        if child["nodeType"] != 1:
            continue
        position += 1
        name = child["localName"]
        path = f"{parent}{name}:nth-child({position})"
        mapping[child["backendNodeId"]] = path
        _backend_selectors(child, path + " > ", mapping)


async def observe(
    page: Page,
    sequence: int,
    *,
    network: list[dict[str, Any]],
    downloads: list[dict[str, Any]],
    dialogs: list[str],
) -> Perception:
    cdp = await page.context.new_cdp_session(page)
    try:
        dom = await _isolated(cdp, f"({_COLLECT})({DESCRIBE})")
        tree = await cdp.send("Accessibility.getFullAXTree")
        document = await cdp.send("DOM.getDocument", {"depth": -1})
    finally:
        await cdp.detach()
    selectors: dict[int, str] = {}
    _backend_selectors(document["root"], "", selectors)
    ax = {}
    pruned = []
    for node in tree["nodes"]:
        if node.get("ignored"):
            continue
        role = node.get("role", {}).get("value", "")
        name = str(node.get("name", {}).get("value", ""))[:160]
        if role not in {"InlineTextBox", "StaticText", "none", "generic"}:
            pruned.append((role, name))
        if selector := selectors.get(node.get("backendDOMNodeId")):
            ax[selector] = (role, name)
    elements = []
    for index, item in enumerate(dom["elements"], 1):
        role, name = ax.get(item["selector"], ("", ""))
        # Keep DOM names for guard checks and use AX names to supplement unnamed controls.
        if role:
            item["role"] = role
        if name and not item["name"]:
            item["name"] = name
        elements.append(Element(index, item))
    digest = sha256(
        json.dumps(
            [pruned, dom["text"], dom["state"], [item.model_fields() for item in elements]],
            sort_keys=True,
            ensure_ascii=True,
        ).encode()
    ).hexdigest()
    projected = []
    for element in elements:
        values = element.model_fields()
        for key in ("href", "form_action"):
            if values[key]:
                values[key] = public_url(values[key])
        values.pop("selector")
        projected.append(values)
    observation = Observation(
        observation_id=sequence,
        url=public_url(page.url),
        title=(await page.title())[:160],
        elements=projected,
        text_excerpt=dom["text"],
        ax_digest=digest,
        network=network[-30:],
        downloads=downloads,
        dialogs=dialogs[-10:],
        tabs=[public_url(tab.url) for tab in page.context.pages if not tab.is_closed()],
    )
    return Perception(
        observation,
        elements,
        DOMSignals(dom["frames"], dom["text"], dom["password"], dom["password"]),
        bool(dom["viewer"]),
    )
