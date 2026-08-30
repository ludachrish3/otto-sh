"""The example project's monitor parsers parse what the bed prints."""

import pytest

from otto.monitor.parsers import ParseContext
from tests.unit.docs.test_getting_started_example import _import_gs_example

_CTX = ParseContext()  # bare construction is the documented test form (parsers.py:75-82)

_NETSTAT = """\
Active Internet connections (servers and established)
Proto Recv-Q Send-Q Local Address           Foreign Address         State
tcp        0      0 0.0.0.0:23              0.0.0.0:*               LISTEN
tcp        0      0 198.51.100.17:23        198.51.100.18:41234     ESTABLISHED
tcp        0      0 198.51.100.17:23        198.51.100.18:41230     TIME_WAIT
tcp        0      0 198.51.100.17:23        198.51.100.18:41231     TIME_WAIT
udp        0      0 0.0.0.0:68              0.0.0.0:*
"""


@pytest.fixture
def parsers():
    """The example's parser classes, imported at run time and never at collection.

    ``gs_example/__init__`` registers process-global extensions the moment it
    is imported -- among them the ``zephyr-inline`` command frame. Importing
    it at module scope would run that registration while pytest is still
    collecting, before ``tests/custom_hosts`` is reached; that module
    registers the same frame unconditionally, so its collection would then
    raise and take the whole session down. Deferring to a fixture keeps
    ``custom_hosts`` first, and ``gs_example``'s own guard makes the second
    registration a no-op.

    Function scope is load-bearing for the root conftest's autouse
    ``_isolate_registries``, which snapshots every otto ``Registry`` per test
    and drops what the test added. As that fixture's own docstring notes,
    pytest sets higher-scoped fixtures up BEFORE function-scoped ones, so at
    module scope these registrations would land inside every per-test snapshot
    and survive the restore for the rest of the session.
    """
    _import_gs_example()
    from gs_example.monitor import BusyBoxSocketsParser, EntropyParser

    return BusyBoxSocketsParser, EntropyParser


def test_entropy_reads_one_integer(parsers):
    _, entropy = parsers
    assert {k: v.value for k, v in entropy().parse("3841\n", ctx=_CTX).items()} == {
        "Entropy": 3841.0
    }


def test_entropy_ignores_garbage(parsers):
    _, entropy = parsers
    assert entropy().parse("cat: can't open", ctx=_CTX) == {}


def test_busybox_sockets_counts_tcp_states(parsers):
    sockets, _ = parsers
    got = {k: v.value for k, v in sockets().parse(_NETSTAT, ctx=_CTX).items()}
    assert got == {"Established": 1.0, "Time-wait": 2.0}


def test_busybox_sockets_replaces_the_ss_command(parsers):
    sockets, _ = parsers
    assert sockets().command == "netstat -tn"
