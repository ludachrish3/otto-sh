"""The shared ``custom_hosts`` module (``tests/custom_hosts``) registers the
``zephyr-inline`` command frame.

``custom_hosts`` emulates a third-party package that SUT repos depend on for
extra shell dialects, pulled in via each repo's ``settings.toml`` ``libs`` +
``init``. Importing it must register ``ZephyrInlineRetcodeFrame`` so *any* repo
(not just repo1) can construct the shared ``embedded`` lab's Zephyr 2.7 host
(``sprout27``, ``command_frame: "zephyr-inline"``) — the regression that the
embedded-lab fold surfaced for repo3.
"""

import sys
from pathlib import Path

import pytest

from otto.host.command_frame import build_command_frame

_CUSTOM_HOSTS = Path(__file__).resolve().parents[2] / "custom_hosts"


@pytest.fixture
def custom_hosts_on_path():
    """Put the third-party module dir on the path and import it (registers)."""
    added = str(_CUSTOM_HOSTS) not in sys.path
    if added:
        sys.path.insert(0, str(_CUSTOM_HOSTS))
    import custom_hosts  # noqa: F401  (import side effect: registers frames)
    yield
    if added:
        while str(_CUSTOM_HOSTS) in sys.path:
            sys.path.remove(str(_CUSTOM_HOSTS))


def test_custom_hosts_registers_zephyr_inline(custom_hosts_on_path):
    frame = build_command_frame("zephyr-inline")
    assert type(frame).__name__ == "ZephyrInlineRetcodeFrame"
