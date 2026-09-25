"""The known-good environments page: rendered from ``otto.check``'s ``proven.json``."""

import re

import pytest

from otto.check.proven import ProvenEntry, ProvenRange
from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic


def test_renders_every_component_and_the_empty_note(tmp_path) -> None:
    from scripts.render_proven_range import render

    text = render()
    assert text.startswith("# Known-good environments")
    for component in ("iproute2", "kernel", "isa", "userland"):
        assert component in text
    assert "ss170501" in text
    assert "6.1.0" in text
    assert "not yet proven" in text
    assert "otto link check" in text
    assert "--report" in text


def _section(text: str, heading: str) -> str:
    """The body of one ``## heading`` section, up to the next ``##``."""
    body = text.split(f"\n## {heading}\n", 1)[1]
    return body.split("\n## ", 1)[0]


def test_an_empty_component_carries_its_own_not_yet_proven_note() -> None:
    """The note is per component: it follows the empty one's heading, and only that one's."""
    from scripts.render_proven_range import render

    proven = ProvenRange(
        revision="r1",
        components={
            "iproute2": [ProvenEntry("6.1.0", "unix bed test1", "2026-09-24")],
            "kernel": [],
            "socat": [],
        },
    )
    text = render(proven)
    kernel = _section(text, "kernel")
    assert "Not yet proven" in kernel
    assert "so every host it fingerprints reads `unknown` for this component" in " ".join(
        kernel.split()
    )
    assert "Compared by version order" in kernel, "link check labels it, so how matters"
    assert "| version |" not in kernel
    iproute2 = _section(text, "iproute2")
    assert "Not yet proven" not in iproute2
    assert "| `6.1.0` | unix bed test1 | 2026-09-24 |" in iproute2


def test_an_empty_component_link_check_never_labels_says_so() -> None:
    """socat/bash/launcher are not labeled by link check, so no host 'reads unknown' for them."""
    from scripts.render_proven_range import render

    text = render(ProvenRange(revision="r1", components={"socat": []}))
    socat = _section(text, "socat")
    assert "Not yet proven" in socat
    assert "does not label this component" in socat
    assert "reads `unknown`" not in socat
    assert "Compared by" not in socat, "no link check compares it, so no comparison is promised"


def test_main_writes_the_page_to_the_output_path(tmp_path) -> None:
    from scripts.render_proven_range import main, render

    out = tmp_path / "known-good.md"
    assert main(["--output", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == render()


def test_the_docs_build_renders_the_page_and_fails_when_the_renderer_does() -> None:
    """The page reaches a reader only because ``docs/conf.py`` runs the renderer.

    Every builder, not html-only: the link toctree names the page, so the doctest
    builder ``make docs`` also runs has to find it on disk. A non-zero exit raises.
    """
    conf = (PROJECT_ROOT / "docs" / "conf.py").read_text(encoding="utf-8")
    assert 'app.connect("builder-inited", _generate_proven_range)' in conf
    hook = conf[conf.index("def _generate_proven_range") :]
    hook = hook[: hook.index("\ndef ")]
    assert '"-m", "scripts.render_proven_range"' in hook
    assert "raise RuntimeError" in hook, "a failed render would only warn"
    assert "app.builder.name" not in hook, "the page is a source file; every builder needs it"


def test_sphinx_srcs_names_the_proven_range_and_its_renderer() -> None:
    """``make docs`` must re-trigger when ``proven.json`` gains a row, like any page edit."""
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    srcs = re.search(r"^SPHINX_SRCS :=.*?\n((?:.*\\\n)*.*)", makefile, re.MULTILINE)
    assert srcs, "no SPHINX_SRCS assignment found in the Makefile (guard misparse?)"
    assert "src/otto/check/proven.json" in srcs.group(0)
    assert "scripts/render_proven_range.py" in srcs.group(0)


def test_the_rendered_page_is_build_output_and_reachable() -> None:
    """Git-ignored (the data file is the one copy), and named by the link toctree."""
    from scripts.render_proven_range import PAGE_PATH

    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "docs/cli/link/known-good.md" in [line.strip() for line in ignored]
    assert PAGE_PATH.relative_to(PROJECT_ROOT).as_posix() == "docs/cli/link/known-good.md"
    index = (PROJECT_ROOT / "docs" / "cli" / "link" / "index.md").read_text(encoding="utf-8")
    assert "known-good" in [line.strip() for line in index.splitlines()]
