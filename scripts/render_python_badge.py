"""Render the docs' "python | 3.10 | 3.11 …" badge from ``pyproject.toml``.

Run from the repo root::

    uv run python -m scripts.render_python_badge           # write the badge
    uv run python -m scripts.render_python_badge --check   # render, write nothing

The overview page shows which Pythons otto supports as a shields.io-style
badge. It used to hotlink img.shields.io, which leaves a broken image in the
offline docs tarball and anywhere else the reader has no internet. This draws
the same flat badge as a self-contained SVG, so it travels with the HTML.

The version list is ``pyproject.toml``'s ``Programming Language :: Python ::
3.X`` classifiers, read by the same :func:`supported_minors` the air-gap wheel
matrix gate uses, so the badge cannot drift from what the package declares.
``docs/conf.py`` runs this at ``builder-inited`` for every builder; the badge
lands in the git-ignored ``docs/_static/generated/``.
"""

import argparse
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from scripts.check_docs_wheel_matrix import supported_minors

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
BADGE_PATH = ROOT / "docs" / "_static" / "generated" / "python-badge.svg"

LABEL = "python"
LABEL_FILL = "#555"
VALUE_FILL = "#007ec6"
"""shields.io's flat-style grey label and its "blue" value colour."""

CHAR_WIDTH = 5.6
PADDING = 6
"""Approximate glyph advance of 11 px Verdana, and the gap either side of a text run.

An estimate, not a font measurement: the badge's text is digits, dots, pipes and
"python", all close to the average advance, so a fixed width keeps the renderer
free of a font dependency while the text still fits its box.
"""


def versions_text(pyproject_text: str) -> str:
    """Return the badge's value text, e.g. ``3.10 | 3.11 | 3.12``."""
    minors = supported_minors(pyproject_text)
    if not minors:
        raise ValueError(
            "pyproject.toml declares no 'Programming Language :: Python :: X.Y' classifier"
        )
    return " | ".join(f"{major}.{minor}" for major, minor in minors)


def _run_width(text: str) -> int:
    return round(len(text) * CHAR_WIDTH) + 2 * PADDING


def render(pyproject_text: "str | None" = None) -> str:
    """Return the badge SVG for the classifiers in ``pyproject_text`` (default: the checkout's)."""
    if pyproject_text is None:
        pyproject_text = PYPROJECT.read_text(encoding="utf-8")
    value = versions_text(pyproject_text)
    label_w, value_w = _run_width(LABEL), _run_width(value)
    width = label_w + value_w
    title = escape(f"{LABEL}: {value}")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="20" '
        f'role="img" aria-label="{title}">'
        f"<title>{title}</title>"
        f'<clipPath id="r"><rect width="{width}" height="20" rx="3"/></clipPath>'
        f'<g clip-path="url(#r)">'
        f'<rect width="{label_w}" height="20" fill="{LABEL_FILL}"/>'
        f'<rect x="{label_w}" width="{value_w}" height="20" fill="{VALUE_FILL}"/>'
        f"</g>"
        f'<g fill="#fff" text-anchor="middle" '
        f'font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">'
        f'<text x="{label_w / 2}" y="14">{escape(LABEL)}</text>'
        f'<text x="{label_w + value_w / 2}" y="14">{escape(value)}</text>'
        f"</g></svg>\n"
    )


def main(argv: "list[str]") -> int:
    """Render the badge, or render it and write nothing."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=BADGE_PATH)
    parser.add_argument("--check", action="store_true", help="render only; write nothing")
    args = parser.parse_args(argv)
    svg = render()
    if args.check:
        print(f"python badge: renders ({versions_text(PYPROJECT.read_text(encoding='utf-8'))})")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(svg, encoding="utf-8")
    print(f"python badge: wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
