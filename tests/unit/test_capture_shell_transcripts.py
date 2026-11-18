"""``make shell-transcripts`` opens every bed guest without needing the recordings it makes.

The capture reads the bed's TOPOLOGY (``tests/conformance/_bed.py``'s
``bed_topology``), never its evidence: a Zephyr guest with no recording yet --
a new guest, or a new release -- must still be opened and recorded, while the
conformance space (``bed_space``) keeps refusing it.
"""

import contextlib
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

import scripts.capture_shell_transcripts as capture
from tests.conformance import _bed, _vocabulary


@pytest.fixture
def no_recordings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every recording missing, as for a bed nothing has been recorded on."""
    # The two cached readers are REPLACED for the test, never cleared: their
    # caches hold the vocabulary objects other modules took at import and
    # compare by identity, so clearing them here would change what a later
    # test in this process sees.
    monkeypatch.setattr(_vocabulary, "recordings", dict)
    monkeypatch.setattr(_vocabulary, "_zephyr_recordings", dict)

    def no_shell(element: str) -> None:
        raise KeyError(element)

    monkeypatch.setattr(_vocabulary, "_zephyr_shell_of", no_shell)


class _OpenedError(Exception):
    """Raised by the fake opener: the capture got as far as opening the cell."""


@pytest.mark.usefixtures("no_recordings")
def test_the_conformance_space_still_refuses_an_unrecorded_zephyr_guest():
    with pytest.raises(ValueError, match="make shell-transcripts"):
        _bed.bed_space()


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_recordings")
async def test_the_capture_opens_every_cell_with_no_recording(monkeypatch: pytest.MonkeyPatch):
    opened: list[str] = []

    def fake(label: str):
        @contextlib.asynccontextmanager
        async def opener() -> "AsyncIterator[object]":
            opened.append(label)
            raise _OpenedError(label)
            yield  # pragma: no cover — never reached; makes this a generator

        return opener

    topology = [
        replace(one, open_host=fake(f"{one.cell.element}-{one.cell.term}"))
        for one in _bed.bed_topology()
    ]
    monkeypatch.setattr(capture, "bed_topology", lambda: topology)
    monkeypatch.setattr(capture, "_install_tee", lambda: None)
    monkeypatch.setattr(capture, "_pooled_test1", fake("test1-ssh-pooled"))

    assert await capture.main() == 1  # every open "failed" with _OpenedError

    zephyr = [f"{c.cell.element}-{c.cell.term}" for c in topology if c.kind == "bed-zephyr"]
    assert zephyr, "the bed's lab data names no Zephyr guest"
    assert set(zephyr) <= set(opened)
    assert opened[-1] == "test1-ssh-pooled"
