"""The input trail: what every page did with focus and text input, kept for a failure.

#1154: on CI webkit, a ``fill()`` into an overlay's label that has just opened
sometimes leaves the field empty, and nothing local reproduces it. The trace
shows the field empty and focused after the fill, but not whether the text
never reached the field or reached it and was reset. The trail answers that.

:data:`INIT_SCRIPT` runs in every page of a dashboard test's browser context
and writes one console line under :data:`PREFIX` for each focus change,
``select``, ``beforeinput``, ``input``, composition event, ``keydown`` and
``click``. Each line names its target by ``data-testid``, tag and a per-element
number, so a field that was REPLACED shows as a new number. A text field's
entry also carries its value; ``beforeinput``, ``input``, ``select`` and
``keydown`` entries carry the selection too. Selection is not read on focus
changes, since that can force a layout while an overlay is still mounting.

How the two causes read:

- no ``beforeinput``/``input`` on the field: the inserted text never reached it;
- an ``input`` carrying the text, and a later ``keydown`` or the final reading
  showing it empty: something reset it (the same number) or replaced the
  field (a new number). A reset of a controlled input fires no event of its
  own, so the trail shows it through the NEXT entry, or the final reading.

:class:`InputTrail` collects those lines on the Python side from the context's
console events. Nothing is read out of the page at failure time but a
final reading bounded at 2 s, so a wedged page cannot stall the teardown,
and entries survive a navigation, a crash, or a page closed before the
report. The lines are in the Playwright trace a failed CI run uploads,
too, in step with the actions.

This records only. It changes no test's actions, waits or retries.
"""

import json
from dataclasses import dataclass, field

from playwright.sync_api import BrowserContext, ConsoleMessage, Error, Page

#: The console prefix that marks a trail line.
PREFIX = "[input-trail] "

#: How long the final reading of a page's focused field may take, in ms.
FINAL_READ_MS = 2_000

INIT_SCRIPT = f"""
(() => {{
  if (window.__ottoInputTrail) return;
  window.__ottoInputTrail = true;
  const t0 = performance.now();
  const ids = new WeakMap();
  let next = 0;
  const name = (el) => {{
    if (!(el instanceof Element)) return el === document ? 'document' : String(el && el.nodeName);
    if (!ids.has(el)) ids.set(el, ++next);
    const owner = el.closest('[data-testid]');
    return (owner ? owner.getAttribute('data-testid') : '-') + '>' + el.tagName.toLowerCase()
      + '#' + ids.get(el);
  }};
  const SELECTION = new Set(['select', 'beforeinput', 'input', 'keydown']);
  const record = (e) => {{
    const target = e.target;
    const entry = {{
      t: Math.round(performance.now() - t0),
      type: e.type,
      target: name(target),
      active: name(document.activeElement),
    }};
    if (target && 'value' in target && typeof target.value === 'string') {{
      entry.value = target.value;
      if (SELECTION.has(e.type)) {{
        try {{ entry.sel = [target.selectionStart, target.selectionEnd]; }} catch (_) {{}}
      }}
    }}
    if (e.inputType) entry.inputType = e.inputType;
    if (e.data != null) entry.data = e.data;
    if (e.key !== undefined) entry.key = e.key;
    console.debug({json.dumps(PREFIX)} + JSON.stringify(entry));
  }};
  for (const type of ['focusin', 'focusout', 'select', 'beforeinput', 'input',
                      'compositionstart', 'compositionend', 'keydown', 'click']) {{
    document.addEventListener(type, record, true);
  }}
}})();
"""


@dataclass
class InputTrail:
    """Every page's trail lines, in arrival order, gathered from console events."""

    entries: dict[int, list[dict]] = field(default_factory=dict)
    pages: list[Page] = field(default_factory=list)

    def install(self, context: BrowserContext) -> None:
        """Record every page the context has now or opens later."""
        context.add_init_script(INIT_SCRIPT)
        context.on("console", self._on_console)

    def _on_console(self, message: ConsoleMessage) -> None:
        if not message.text.startswith(PREFIX):
            return
        page = message.page
        if page is None:
            return
        if page not in self.pages:
            self.pages.append(page)
        index = self.pages.index(page)
        self.entries.setdefault(index, []).append(json.loads(message.text[len(PREFIX) :]))

    def of(self, page: Page) -> list[dict]:
        """The trail one page has written so far."""
        return self.entries.get(self.pages.index(page), []) if page in self.pages else []

    def report(self) -> str:
        """Every page's trail, oldest first, then what its focused field holds now.

        Walks every page that wrote a line, closed ones included (a second tab
        a fixture closed before this teardown). The final reading comes
        first, so lines that arrive while it runs are in the printed trail.
        """
        lines: list[str] = []
        for page in self.pages:
            final = "page closed" if page.is_closed() else _final_reading(page)
            trail = self.of(page)
            lines.append(f"page {page.url}: {len(trail)} entries")
            lines.extend(f"  {json.dumps(entry)}" for entry in trail)
            lines.append(f"  final: {final}")
        return "\n".join(lines)


def _final_reading(page: Page) -> str:
    """The focused field's testid and value now, bounded so a wedged page cannot stall."""
    focused = page.locator(":focus")
    try:
        testid = focused.get_attribute("data-testid", timeout=FINAL_READ_MS)
        value = focused.input_value(timeout=FINAL_READ_MS)
    except Error as exc:
        return f"unreadable ({type(exc).__name__}: {str(exc).splitlines()[0]})"
    return json.dumps({"focused": testid, "value": value})
