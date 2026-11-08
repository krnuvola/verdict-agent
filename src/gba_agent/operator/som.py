"""Viewport screenshots with numbered marks and masked form controls."""

from playwright.async_api import Page

from gba_agent.operator.perception import Element

_MARKS = """boxes => {
    const layer = document.createElement('div');
    layer.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647';
    for (const {index, box} of boxes) {
        const {x,y,width,height} = box;
        if (x + width <= 0 || y + height <= 0 || x >= innerWidth || y >= innerHeight) continue;
        const mark = document.createElement('span');
        mark.textContent = String(index);
        mark.style.cssText = 'position:absolute;font:bold 12px sans-serif;background:#222;'
            + 'color:white;padding:2px 4px;border:1px solid white';
        mark.style.left = Math.max(0,x) + 'px';
        mark.style.top = Math.max(0,y) + 'px';
        layer.append(mark);
    }
    document.documentElement.append(layer);
    return layer;
}"""


async def marked_screenshot(page: Page, elements: list[Element]) -> bytes:
    layer = await page.evaluate_handle(
        _MARKS, [{"index": element.index, "box": element.data["box"]} for element in elements]
    )
    try:
        return await page.screenshot(
            type="png",
            timeout=5000,
            mask=[page.locator("input, textarea, select, [contenteditable], iframe")],
        )
    finally:
        await layer.evaluate("layer => layer.remove()")
        await layer.dispose()
