"""The A2 measurement opens a bed cell as a chosen login.

No lab host is reached: the hosts are built, never opened.
"""

import pytest

from tests.conformance._bed import BED_TECH, bed_topology, build_bed_host
from tests.conformance._lab_context import bed_lab_context


def _cell(element: str, term: str):
    return next(c.cell for c in bed_topology() if c.cell.element == element and c.cell.term == term)


@pytest.mark.parametrize("login", ["vagrant", "test"])
def test_a_bed_host_built_for_a_login_puts_that_login_first(login: str) -> None:
    with bed_lab_context(BED_TECH):
        host = build_bed_host(_cell("test1", "ssh"), login=login)
    assert host.creds[0].login == login
    assert sorted(c.login for c in host.creds) == ["test", "vagrant"]


def test_no_login_keeps_the_lab_s_order() -> None:
    with bed_lab_context(BED_TECH):
        host = build_bed_host(_cell("test1", "ssh"))
    assert [c.login for c in host.creds] == ["vagrant", "test"]


def test_a_login_the_lab_entry_lacks_is_refused() -> None:
    with bed_lab_context(BED_TECH), pytest.raises(ValueError, match="no login 'nobody'"):
        build_bed_host(_cell("test1", "ssh"), login="nobody")
