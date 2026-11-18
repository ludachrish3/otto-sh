"""The input trail tells #1154's two causes apart: text never arriving, or arriving then lost."""

import pytest
from playwright.sync_api import BrowserContext, Page

from tests.e2e.monitor.dashboard._input_trail import InputTrail

pytestmark = [pytest.mark.hostless, pytest.mark.browser]

_ADD_FIELD = """() => {
  const field = document.createElement('input');
  field.setAttribute('data-testid', 'trail-probe');
  document.body.append(field);
}"""


def _probe_entries(trail: InputTrail, page: Page) -> list[dict]:
    return [entry for entry in trail.of(page) if entry["target"].startswith("trail-probe>input#")]


def test_a_fill_shows_focus_then_the_text_arriving(
    page: Page, context: BrowserContext, input_trail: InputTrail, shell_dash
) -> None:
    page.goto(shell_dash.url)
    page.evaluate(_ADD_FIELD)
    page.get_by_test_id("trail-probe").fill("db note")
    page.wait_for_function("() => document.activeElement?.value === 'db note'")

    mine = _probe_entries(input_trail, page)
    kinds = [entry["type"] for entry in mine]
    assert "focusin" in kinds, mine
    assert "input" in kinds, mine
    assert kinds.index("focusin") < kinds.index("input"), mine
    arrived = next(entry for entry in mine if entry["type"] == "input")
    assert arrived["value"] == "db note"
    assert arrived["active"] == arrived["target"]
    assert "sel" not in mine[kinds.index("focusin")], "selection is never read on a focus change"

    second = context.new_page()
    second.goto(shell_dash.url)
    second.evaluate(_ADD_FIELD)
    second.get_by_test_id("trail-probe").fill("tab b")
    second.wait_for_function("() => document.activeElement?.value === 'tab b'")
    second.close()

    report = input_trail.report()
    assert report.startswith(f"page {page.url}: ")
    assert '"value": "tab b"' in report, "a page closed before the report still reports"
    assert report.rstrip().endswith("final: page closed")
    assert 'final: {"focused": "trail-probe", "value": "db note"}' in report


def test_a_reset_after_the_fill_shows_as_the_next_entry_and_a_remount_as_a_new_element(
    page: Page, input_trail: InputTrail, shell_dash
) -> None:
    page.goto(shell_dash.url)
    page.evaluate(_ADD_FIELD)
    field = page.get_by_test_id("trail-probe")
    field.fill("db note")
    # A controlled input's reset: the value goes back with no event of its own.
    page.evaluate("() => { document.querySelector('[data-testid=trail-probe]').value = ''; }")
    field.press("Enter")
    # A remount: the same testid on a fresh element.
    page.evaluate("() => { document.querySelector('[data-testid=trail-probe]').remove(); }")
    page.evaluate(_ADD_FIELD)
    page.get_by_test_id("trail-probe").focus()
    page.wait_for_function(
        "() => document.activeElement?.getAttribute('data-testid') === 'trail-probe'"
    )

    mine = _probe_entries(input_trail, page)
    arrived = next(entry for entry in mine if entry["type"] == "input")
    enter = next(entry for entry in mine if entry["type"] == "keydown" and entry["key"] == "Enter")
    assert arrived["value"] == "db note"
    assert enter["value"] == "", "the reset shows in the entry after it"
    assert enter["target"] == arrived["target"], "the same element: reset, not replaced"
    refocus = [entry for entry in mine if entry["type"] == "focusin"][-1]
    assert refocus["target"] != arrived["target"], "the replacement field has a new number"

    assert input_trail.report().rstrip().endswith('final: {"focused": "trail-probe", "value": ""}')
