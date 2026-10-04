"""The known-good environments page: rendered from ``otto.check``'s ``proven.json``."""

import re

import pytest

from otto.check.proven import ProvenEntry, ProvenRange
from tests._fixtures.docs_conf import assert_conf_renders
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
    assert "otto tunnel check" in text
    assert "planned" not in text, "the tunnel check has landed; nothing is planned any more"
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
    assert "so every host they fingerprint reads `unknown` for this component" in " ".join(
        kernel.split()
    ), "kernel is labeled by both checks, so the verb agrees with two"
    assert "Compared by version order" in kernel, "a check labels it, so how matters"
    assert "| version |" not in kernel
    iproute2 = _section(text, "iproute2")
    assert "Not yet proven" not in iproute2
    assert "| `6.1.0` | unix bed test1 | 2026-09-24 |" in iproute2


def _note(text: str, heading: str) -> str:
    """One section's prose on a single line, so a wrapped phrase still matches."""
    return " ".join(_section(text, heading).split())


def test_an_empty_note_names_every_check_that_labels_the_component() -> None:
    """kernel is labeled by both checks, iproute2 by link check only, socat by tunnel check only."""
    from scripts.render_proven_range import render

    text = render(
        ProvenRange(revision="r1", components={"iproute2": [], "kernel": [], "socat": []})
    )
    assert "`otto link check` and `otto tunnel check` label this component" in _note(text, "kernel")
    assert "`otto link check` labels this component" in _note(text, "iproute2")
    assert "otto tunnel check" not in _note(text, "iproute2")
    socat = _note(text, "socat")
    assert "`otto tunnel check` labels this component" in socat
    assert "otto link check" not in socat


def test_socat_is_labeled_and_compared_by_version_order() -> None:
    """socat was recorded-only while the tunnel check was planned; now a check labels it."""
    from scripts.render_proven_range import render

    socat = _note(render(ProvenRange(revision="r1", components={"socat": []})), "socat")
    assert "Compared by version order" in socat
    assert "so every host it fingerprints reads `unknown`" in socat, "one check labels it"
    assert "does not label this component" not in socat


def test_a_component_no_check_labels_says_so() -> None:
    """A component in proven.json that neither check labels promises no comparison."""
    from scripts.render_proven_range import render

    text = render(ProvenRange(revision="r1", components={"tc-flavour": []}))
    note = _note(text, "tc-flavour")
    assert "Not yet proven" in note
    assert "no check labels this component" in note
    assert "Compared by" not in note
    assert "reads `unknown`" not in note


def test_the_labeled_sets_are_the_checks_own() -> None:
    """The page's claims about who labels what are the two checks' own lists, not a copy."""
    from otto.link import check as link_check
    from otto.tunnel import _tunnel_render as tunnel_render
    from scripts.render_proven_range import LINK_CHECK_COMPONENTS, TUNNEL_CHECK_COMPONENTS

    assert LINK_CHECK_COMPONENTS == link_check._LABELLED
    assert TUNNEL_CHECK_COMPONENTS == tunnel_render.RANGE_COMPONENTS


def test_the_report_call_to_action_names_both_checks() -> None:
    from scripts.render_proven_range import render

    report = _note(render(), "Sending a report")
    assert "otto link check <link> --report check.json" in report
    assert "otto tunnel check --hosts" in report
    assert "--report check.json" in report
    assert "saying which command you ran" in " ".join(report.split())
    assert "(check-verdicts.md#the-report-file)" in report


def test_the_page_links_resolve_from_its_new_home() -> None:
    """The page sits in docs/cli/, beside check-verdicts.md: its links are relative to that."""
    from scripts.render_proven_range import render

    text = render()
    assert "(link/check.md)" in text
    assert "(tunnel/check.md)" in text
    assert "../check-verdicts.md" not in text
    assert "(check-verdicts.md#proven-range-labels)" in text


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
    assert_conf_renders("scripts.render_proven_range", "docs/cli/known-good.md")


def test_sphinx_srcs_names_the_proven_range_and_its_renderer() -> None:
    """``make docs`` must re-trigger when ``proven.json`` gains a row, like any page edit."""
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    srcs = re.search(r"^SPHINX_SRCS :=.*?\n((?:.*\\\n)*.*)", makefile, re.MULTILINE)
    assert srcs, "no SPHINX_SRCS assignment found in the Makefile (guard misparse?)"
    assert "src/otto/check/proven.json" in srcs.group(0)
    assert "scripts/render_proven_range.py" in srcs.group(0)


def test_the_rendered_page_is_build_output_and_reachable() -> None:
    """Git-ignored (the data file is the one copy), and named by the CLI topics toctree.

    One home for both checks: it sits in docs/cli/, not under either check's verb.
    """
    from scripts.render_proven_range import PAGE_PATH

    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "docs/cli/known-good.md" in [line.strip() for line in ignored]
    assert "docs/cli/link/known-good.md" not in [line.strip() for line in ignored]
    assert PAGE_PATH.relative_to(PROJECT_ROOT).as_posix() == "docs/cli/known-good.md"
    index = (PROJECT_ROOT / "docs" / "cli" / "index.md").read_text(encoding="utf-8")
    assert "known-good" in [line.strip() for line in index.splitlines()]
    link_index = (PROJECT_ROOT / "docs" / "cli" / "link" / "index.md").read_text(encoding="utf-8")
    assert "known-good" not in [line.strip() for line in link_index.splitlines()]
