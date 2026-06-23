"""The shared ``custom_hosts`` module (``tests/custom_hosts``) registers the
``zephyr-inline`` command frame.

``custom_hosts`` emulates a third-party package that SUT repos depend on for
extra shell dialects, pulled in via each repo's ``settings.toml`` ``libs`` +
``init``. Importing it must register ``ZephyrInlineRetcodeFrame`` so *any* repo
(not just repo1) can construct the shared ``embedded`` lab's Zephyr 2.7 host
(``sprout27``, ``command_frame: "zephyr-inline"``) — the regression that the
embedded-lab fold surfaced for repo3.
"""

import pytest

from otto.host.command_frame import build_command_frame
from tests._fixtures.paths import ensure_custom_hosts_on_path

ensure_custom_hosts_on_path()


@pytest.fixture
def custom_hosts_on_path():
    """Put the third-party module dir on the path and import it (registers)."""
    import custom_hosts  # noqa: F401  (import side effect: registers frames)
    yield


def test_custom_hosts_registers_zephyr_inline(custom_hosts_on_path):
    frame = build_command_frame("zephyr-inline")
    assert type(frame).__name__ == "ZephyrInlineRetcodeFrame"
