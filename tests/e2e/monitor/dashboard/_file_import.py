"""Hand the dashboard a file to import, and settle what the import decides.

An Import the tab ignores leaves nothing to wait for, so a check right after
it would pass before the import had run. :func:`track_blob_reads` records
every ``Blob.text()`` read the page starts (``File`` inherits it), and
:func:`settle_blob_reads` waits for them, then two animation frames for React
to commit what the reads' continuations set. The shell's own continuation,
which decides the import, is queued ahead of the settle's, so a check after
the settle sees the decision.
"""

from pathlib import Path

from playwright.sync_api import Page

# Wraps Blob.prototype.text (File inherits it) to keep each read's promise.
_TRACK_BLOB_READS_JS = """() => {
    const reads = [];
    const original = Blob.prototype.text;
    Blob.prototype.text = function () {
        const read = original.call(this);
        reads.push(read);
        return read;
    };
    window.__ottoBlobReads = reads;
}"""

# Awaits every recorded read; resolves the count.
_AWAIT_BLOB_READS_JS = """async () => {
    const reads = window.__ottoBlobReads;
    await Promise.allSettled(reads);
    return reads.length;
}"""

# Resolves after two animation frames.
_TWO_FRAMES_JS = """async () => {
    for (let i = 0; i < 2; i += 1) {
        await new Promise((resolve) => requestAnimationFrame(() => resolve()));
    }
}"""

# Records, in the capture phase ahead of every app listener, how many files
# the next drop on the window carries.
_WATCH_DROP_JS = """() => {
    window.__ottoDropFiles = null;
    window.addEventListener(
        "drop",
        (e) => { window.__ottoDropFiles = e.dataTransfer?.files?.length ?? 0; },
        { capture: true, once: true },
    );
}"""

# A DataTransfer holding one JSON File, for a synthetic drop.
_DATA_TRANSFER_JS = """([name, text]) => {
    const transfer = new DataTransfer();
    transfer.items.add(new File([text], name, { type: "application/json" }));
    return transfer;
}"""


def track_blob_reads(page: Page) -> None:
    """Start recording every ``Blob.text()`` read *page* makes."""
    page.evaluate(_TRACK_BLOB_READS_JS)


def settle_frames(page: Page) -> None:
    """Wait two animation frames, so React has committed what ran before."""
    page.evaluate(_TWO_FRAMES_JS)


def settle_blob_reads(page: Page) -> int:
    """Wait for every recorded read and two frames; return how many reads started."""
    reads = page.evaluate(_AWAIT_BLOB_READS_JS)
    settle_frames(page)
    return reads


def drop_file(page: Page, path: Path) -> int:
    """Drop *path* onto *page*'s window, as a file dragged from the desktop.

    Returns how many files the drop event the window received carried, so the
    caller can assert the drop delivered its file: an engine that dropped
    an empty DataTransfer must fail a test, not pass it vacuously.
    """
    transfer = page.evaluate_handle(
        _DATA_TRANSFER_JS, [path.name, path.read_text(encoding="utf-8")]
    )
    page.evaluate(_WATCH_DROP_JS)
    page.dispatch_event("body", "drop", {"dataTransfer": transfer})
    return page.evaluate("() => window.__ottoDropFiles")
