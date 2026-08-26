"""The real progress handler on a recording console: the bar the user sees, pinned.

Everything else in this workstream pins the EVENTS a backend emits. This file
is the only place that pins what those events RENDER, which is the thing a
user actually watches: one row per file, a bar that reaches full, and a
byte counter that ends at the file's own size.

Three rendering assumptions are load-bearing for the single-line assertions
below; all three were measured against this repo's Rich (15.0.0) INSIDE a
pytest run on 2026-08-26 rather than assumed:

* the console really is 100 columns, which fits description + 40-glyph bar +
  download + speed + time on ONE line (the widest row here is the
  mid-transfer one at 84 columns, so nothing wraps and ``"…" in row`` never
  straddles a line break). ``width=100`` ALONE does not buy that under
  pytest -- see ``_render`` below.
* ``progress.tasks`` is in add-order, so ``rows[0]``/``rows[1]`` below are
  a.bin then b.bin -- the order their first events arrived in.
* ``color_system=None`` strips the product's ``[green]``/``[bold blue]``
  markup out of ``export_text()``. The markup survives on
  ``task.description`` (it is the string the product passed to
  ``add_task``), which is why these tests read the exported TEXT and not
  that attribute.

The Live's export carries frames and cursor escapes, so a single frame is
rendered via ``progress.get_renderable()`` instead of entering the Live.
"""

from rich.console import Console
from rich.progress import Progress

from otto.host.transfer.progress import make_rich_progress_handler, make_transfer_progress
from tests._fixtures.progress import ProgressEvent, assert_progress_invariants, events_for

_FULL = "━"  # BarColumn's filled glyph; the partial one is "╸" and is NOT this
_BAR = _FULL * 40  # BarColumn's default width; a finished bar is all full glyphs


def _render(stream: list[ProgressEvent]) -> "tuple[Progress, str]":
    """Drive the REAL handler with *stream* and return its one rendered frame.

    ``height`` is pinned deliberately and is NOT cosmetic. tests/conftest.py
    sets ``TERM=dumb`` process-wide, and rich's ``Console.size`` returns a
    hardcoded ``(80, 25)`` for a dumb terminal -- ignoring ``width=100`` --
    unless BOTH dimensions are given, in which case it short-circuits on them
    first. With only ``width=100`` the console is 80 columns here and the bar
    column absorbs the squeeze -- but only TWO assertions below red for it,
    both glyph counts on an ``a.bin`` row: the finished bar drops from 40 to
    36-37 and the mid-transfer one from 26 to 23-24. The ``empty.bin`` row is
    77 columns, fits inside 80, and never squeezes, so its ``_BAR`` assertion
    survives; ``test_each_file_gets_its_own_row`` asserts no bar at all.
    Pinning both makes the viewport independent of the ambient ``TERM``.

    Those squeezed numbers are RANGES because ``TransferSpeedColumn`` renders
    a variable-width field -- ``?`` before it has a sample, else something
    like ``9.1 GB/s`` or ``11.9 GB/s`` -- which swings a row by up to ~7
    columns and, once the console is squeezing, moves the bar by a glyph or
    two with it. At the pinned width there is slack (the widest row is 84 of
    100), so the bar keeps its fixed 40 and the counts asserted below are
    exact, not ranges. This is NOT a live flake here; it is why a bare
    ``python`` probe, on a console the suite never renders on, can report a
    squeezed count the suite does not.
    """
    console = Console(record=True, width=100, height=25, force_terminal=True, color_system=None)
    progress = Progress(*make_transfer_progress().columns, console=console, auto_refresh=False)
    handler = make_rich_progress_handler(progress, "host-a")
    for e in stream:
        handler(e.src, e.dst, e.done, e.total)
    console.print(progress.get_renderable())
    return progress, console.export_text()


def test_a_finished_file_renders_full_with_its_total():
    # 49169 is 3*G + 17 at G=16384: four events, the last one partial.
    stream = events_for(src="/local/a.bin", total=49169, granularity=16384)
    assert_progress_invariants(stream, src="/local/a.bin", total=49169, granularity=16384)
    progress, text = _render(stream)
    (task,) = progress.tasks
    assert task.completed == 49169
    assert task.finished
    (row,) = [ln for ln in text.splitlines() if "a.bin" in ln]
    assert "host-a a.bin" in row, row
    assert _BAR in row, row
    assert "49.2/49.2 kB" in row, row


def test_each_file_gets_its_own_row():
    # Two DIFFERENT paths through events_for on purpose: a.bin is multi-chunk
    # (four events at G=16384), b.bin is at its own stride so events_for
    # COLLAPSES it to exactly ONE event. A single-event file is the harder
    # case for the handler's src-change branch, since the row must appear
    # from one call.
    stream = events_for(src="/local/a.bin", total=49169, granularity=16384) + events_for(
        src="/local/b.bin", total=8192, granularity=8192
    )
    # Split by src BEFORE checking invariants: assert_progress_invariants
    # takes ONE file's stream and its clause 2 REFUSES a foreign src, so the
    # concatenated list would red naming the other file.
    assert_progress_invariants(
        [e for e in stream if e.src == "/local/a.bin"],
        src="/local/a.bin",
        total=49169,
        granularity=16384,
    )
    assert_progress_invariants(
        [e for e in stream if e.src == "/local/b.bin"],
        src="/local/b.bin",
        total=8192,
        granularity=8192,
    )
    progress, text = _render(stream)
    assert [t.finished for t in progress.tasks] == [True, True]
    rows = [ln for ln in text.splitlines() if ".bin" in ln]
    assert len(rows) == 2, rows
    assert "49.2/49.2 kB" in rows[0], rows
    assert "8.2/8.2 kB" in rows[1], rows


def test_an_empty_file_renders_finished_not_stuck():
    # Built by hand, not by events_for: a 0-byte payload is outside the
    # invariant contract (both helpers refuse total <= 0), but it is very much
    # inside what a real transfer hands the handler.
    progress, text = _render([ProgressEvent(src="/local/empty.bin", dst="", done=0, total=0)])
    (task,) = progress.tasks
    assert task.finished, "a 0/0 task must finish, or the bar sits forever"
    (row,) = [ln for ln in text.splitlines() if "empty.bin" in ln]
    assert _BAR in row, row
    assert "0/0 bytes" in row, row


def test_a_mid_transfer_frame_is_partial():
    stream = events_for(src="/local/a.bin", total=49169, granularity=16384)[:2]  # 32768 of 49169
    progress, text = _render(stream)
    (task,) = progress.tasks
    assert not task.finished
    assert task.completed == 32768
    (row,) = [ln for ln in text.splitlines() if "a.bin" in ln]
    # The bar's FULLNESS is not falsifiable on its own -- a bar is full exactly
    # when completed >= total, so anything that fills it mid-transfer also
    # finishes the task, and `not task.finished` above reds first. Its GLYPH
    # COUNT is: 26 is the measured fill of the 40-glyph bar at 32768/49169
    # (0.666 * 40 floored, plus one partial "╸" this does not count), and it
    # was 26 in 200 of 200 renders inside pytest on 2026-08-26. A
    # `BarColumn(bar_width=20)` mutation renders 13 here while both asserts
    # above stay green, so this line -- not `_BAR not in row` -- is what
    # carries the red for how full the user's bar looks.
    assert row.count(_FULL) == 26, row
    assert "32.8/49.2 kB" in row, row
