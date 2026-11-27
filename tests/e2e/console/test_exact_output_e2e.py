"""A command's output comes back exactly from an ssh ``exec``, every time (#711).

``CommandResult.value`` is the command's output with exactly one trailing
newline removed. asyncssh's exec channel handed back an empty partial line
when end-of-file landed after the last line (#711): a timing defect, so a
single run proves nothing. An ``exec(..., terminal=False)`` runs on a pooled
shell session without a terminal, the session otto's own commands take on a
direct ssh host, and this module runs each command 20 times on it. An unset
``terminal`` would take the session with a terminal instead.

One run of each case on every term, route and log mode is the conformance
suite's: the value in ``tests/conformance/test_output_contract.py``, what the
host log receives in ``tests/conformance/test_log_contract.py`` (the
``exec-log`` surface, which owns the log). The per-repeat check that no empty
log line was added is gone from here: the defect that added one also
corrupted ``value``, which every repeat still asserts.
"""

import pytest

from otto.host.host import Host
from otto.testing import EXACT_OUTPUT_CASES

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    # Every command 20 times over the session.
    pytest.mark.timeout(75),
]

_REPEATS = 20
"""Runs per command on the ssh exec session: enough for a 1-in-20 timing flake to show."""


def _report(failures: list[str]) -> str:
    shown = failures[:60]
    more = f"\n... and {len(failures) - len(shown)} more" if len(failures) > len(shown) else ""
    return f"{len(failures)} mismatch(es):\n" + "\n".join(shown) + more


@pytest.mark.parametrize("host1", ["ssh"], indirect=True)
async def test_an_ssh_exec_answers_the_exact_output_every_time(host1: Host) -> None:
    """Every shipped case, 20 times on the session without a terminal, answers its exact value."""
    assert not host1._shares_one_line()
    assert host1._exec_plain(user=None, needs_shell=False) is True
    failures: list[str] = []
    for case in EXACT_OUTPUT_CASES:
        for iteration in range(1, _REPEATS + 1):
            result = await host1.exec(case.command, terminal=False)
            if (result.value, result.retcode) != (case.value, case.retcode):
                failures.append(
                    f"cmd={case.command!r} iteration={iteration}/{_REPEATS}: got "
                    f"({result.value!r}, {result.retcode}), expected ({case.value!r}, "
                    f"{case.retcode})"
                )
    assert not failures, _report(failures)
