"""Helpers shared by the coverage-report browser tests.

Two jobs live here, each in exactly one place: rewriting the payload as
``window.__OTTO_COV__`` is assigned (so a test can put any data in front of
the real built bundle), and measuring the WCAG contrast of rendered text
against whatever it is composited over.
"""

import json

from playwright.sync_api import Locator, Page

# Computed colours come back in whatever space the stylesheet used (oklch for
# the theme tokens, `color(srgb ...)` for `color-mix`), so the browser itself
# rasterises each one to sRGB 0-1 + alpha via a 1x1 canvas.
TO_RGBA_JS = """colors => {
    const ctx = document.createElement('canvas').getContext('2d', {willReadFrequently: true});
    return colors.map(c => {
        ctx.clearRect(0, 0, 1, 1);
        // An unparseable colour leaves fillStyle unchanged: seed a sentinel
        // so that shows up as a failure rather than as black.
        const sentinel = 'rgba(1, 2, 3, 0.5)';
        ctx.fillStyle = sentinel;
        ctx.fillStyle = c;
        if (ctx.fillStyle === sentinel) throw new Error('unparseable colour: ' + c);
        ctx.fillRect(0, 0, 1, 1);
        const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
        return [r / 255, g / 255, b / 255, a / 255];
    });
}"""


def rewrite_payload(page: Page, transform_js: str) -> None:
    """Rewrite the payload as ``window.__OTTO_COV__`` is assigned.

    ``transform_js`` is a JS function expression ``(payload) => payload``;
    it runs on the parsed value before the app ever reads it.

    Only one rewrite can apply per page: a second call defines the setter
    again and replaces the first, so combine edits into one transform.
    """
    page.add_init_script(
        f"""(() => {{
            const transform = {transform_js};
            Object.defineProperty(window, "__OTTO_COV__", {{
                configurable: true,
                set(v) {{
                    Object.defineProperty(window, "__OTTO_COV__", {{
                        value: transform(JSON.parse(JSON.stringify(v))),
                        writable: true, configurable: true,
                    }});
                }},
            }});
        }})();"""
    )


def swap_payload_strings(page: Page, swaps: list[tuple[str, str]]) -> None:
    """Replace every occurrence of each ``old`` with ``new`` in the payload's JSON."""
    rewrite_payload(
        page,
        f"""(v) => {{
            let s = JSON.stringify(v);
            for (const [a, b] of {json.dumps(swaps)}) s = s.split(a).join(b);
            return JSON.parse(s);
        }}""",
    )


def set_state_colors(page: Page, colors: dict[str, str]) -> None:
    """Override ``index.state_colors`` entries (stale / aging / excluded)."""
    rewrite_payload(
        page,
        f"(v) => {{ Object.assign(v.state_colors, {json.dumps(colors)}); return v; }}",
    )


def _over(
    top: tuple[float, float, float, float], below: tuple[float, float, float]
) -> tuple[float, float, float]:
    r, g, b, alpha = top
    return (
        r * alpha + below[0] * (1 - alpha),
        g * alpha + below[1] * (1 - alpha),
        b * alpha + below[2] * (1 - alpha),
    )


def _luminance(rgb: tuple[float, float, float]) -> float:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def text_contrast(page: Page, element: Locator) -> tuple[str, float]:
    """The element's text colour and its contrast against what it sits on.

    The backdrop is the element's own background and every ancestor's,
    innermost first, composited in reverse over white.
    """
    text, backgrounds = element.evaluate(
        """el => {
            const chain = [];
            for (let n = el; n; n = n.parentElement) {
                chain.push(getComputedStyle(n).backgroundColor);
            }
            return [getComputedStyle(el).color, chain];
        }"""
    )
    text_rgba, *bg_rgbas = page.evaluate(TO_RGBA_JS, [text, *backgrounds])
    backdrop = (1.0, 1.0, 1.0)
    for bg in reversed(bg_rgbas):
        backdrop = _over(tuple(bg), backdrop)
    return text, contrast_ratio(tuple(text_rgba[:3]), backdrop)


def background_rgb(page: Page, element: Locator) -> tuple[float, float, float]:
    """The element's own background colour as sRGB 0-1."""
    (rgba,) = page.evaluate(
        TO_RGBA_JS, [element.evaluate("el => getComputedStyle(el).backgroundColor")]
    )
    return (rgba[0], rgba[1], rgba[2])


def hex_rgb(color: str) -> tuple[float, float, float]:
    """``#rrggbb`` as sRGB 0-1."""
    return tuple(int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]
