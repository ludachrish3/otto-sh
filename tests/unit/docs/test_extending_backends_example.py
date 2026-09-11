"""The custom-backend example on ``docs/library/extending-backends.md`` still runs.

A page that teaches how to write a transfer backend publishes a signature,
and a signature drifts silently: the prose above the block gets updated, the
block does not, and the next reader copies a class the base class refuses to
call. So the fence is not read here, it is EXECUTED -- ``exec``'d as the page
spells it, then put through
:func:`~otto.testing.assert_transfer_backend_conforms` and one real
``put_files`` of two files.

The example's transfer body is an elided ``...`` (a real one would need a
serial line), so no byte moves and the destination is never created.
Everything around it is real: the class is instantiated, the base's
``put_files`` runs filename validation and the dispatcher for real, and the
per-file mapping the page promises is the mapping asserted here.
"""

import asyncio
import re

import pytest

from otto.host.transfer import TRANSFER_BACKENDS
from otto.host.transfer import registry as registry_mod
from otto.testing import assert_transfer_backend_conforms
from otto.utils import Status
from tests._fixtures.paths import PROJECT_ROOT

_PAGE = PROJECT_ROOT / "docs" / "library" / "extending-backends.md"
_FENCE = re.compile(r"^```python\n(?P<body>.*?)^```$", re.MULTILINE | re.DOTALL)


def _example_source() -> str:
    """The one fenced block on the page that defines the worked-example class."""
    blocks = [m.group("body") for m in _FENCE.finditer(_PAGE.read_text())]
    matching = [b for b in blocks if "class XmodemTransfer" in b]
    assert len(matching) == 1, (
        f"expected exactly one XmodemTransfer fence in {_PAGE.name}, found "
        f"{len(matching)} among {len(blocks)} python fences"
    )
    return matching[0]


@pytest.fixture
def example_class(monkeypatch):
    """Execute the page's fence and hand back the class it defines.

    ``register_transfer_backend`` stays the real function, pointed at a
    scratch registry: the example's registration call is part of what the
    page teaches and must run, without leaking an ``xmodem`` name into the
    process-wide registry every other test reads.
    """
    scratch = type(TRANSFER_BACKENDS)(
        kind="transfer backend",
        register_hint="register_transfer_backend(name, cls)",
    )
    monkeypatch.setattr(registry_mod, "TRANSFER_BACKENDS", scratch)
    namespace: dict = {}
    exec(compile(_example_source(), str(_PAGE), "exec"), namespace)  # noqa: S102
    assert scratch.names() == ["xmodem"], "the example's registration call did not run"
    return namespace["XmodemTransfer"]


def test_the_example_backend_satisfies_the_backend_contract(example_class):
    """The published example is a backend otto would actually accept.

    This is the check that goes red when an abstract hook's signature moves
    under the page -- ``concurrent`` by keyword is exactly such a move.
    """
    assert_transfer_backend_conforms(example_class)


def test_the_example_backend_transfers_a_batch(example_class, tmp_path):
    """``put_files`` drives the example's own hooks and answers per file.

    Two files, not one: the example hands a closure to the base dispatcher
    rather than writing its own loop, and a single-file batch would not
    exercise that at all.
    """
    src_files = [tmp_path / "a.bin", tmp_path / "b.bin"]
    for f in src_files:
        f.write_bytes(b"x")
    dest_dir = tmp_path / "remote"

    result = asyncio.run(
        example_class(name="mote").put_files(src_files, dest_dir, show_progress=False)
    )

    assert result.status is Status.Success, result.msg
    assert set(result.value) == set(src_files)
    assert [result.value[f].value for f in src_files] == [dest_dir / f.name for f in src_files]
