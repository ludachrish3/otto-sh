"""Stability/soak test for the AppShell lifecycle against the live mysql bed.

Companion to ``tests/e2e/host/test_app_shell_e2e.py`` (which proves the
``mysql`` app-shell story works at all) — this module re-runs the SAME shape
of assertion under ``pytest-repeat`` (``--count``) via ``make stability-unix``
to flush intermittent flakiness in:

- launching the interactive ``mysql`` client through the ``sudo-su-shell``
  login proxy and driving it prompt-framed (:meth:`~otto.AppShell.cmd`);
- parsing real bordered ``mysql --pager=cat`` output into a nested
  :class:`Select`/:class:`Row`/:class:`QueryStats` object graph;
- ``_recover_session``-on-exit — the host's OWN default session must still be
  usable after the app shell quits, every time, not just once.

Containment (mirrors ``test_app_shell_e2e.py`` and
``test_proxy_user_stability_integration.py`` exactly, see either module's
docstring for the full rationale): the ``sudo-su-shell`` login proxy is
registered at MODULE scope below (``overwrite=True`` so re-import under
xdist and co-import with the e2e / proxy-stability modules are all
idempotent), and ``MySql``/``Row``/``QueryStats``/``Select``/
``_MYSQL_CREDS``/``_mysql_host_dict`` are redefined locally rather than
imported from the e2e module. Hosts are built from inline dicts; only the
leased VM's IP is read from ``tech1/lab.json`` (via
:func:`tests._fixtures.labdata.host_data`) — never its creds.

mysql connection note
---------------------
The ``mysql`` client resolves its *default* DB user from the terminal login
name (``getlogin()`` / utmp = the SSH login, ``vagrant``), NOT from the
``su``'d euid (``mysql``), so a bare ``mysql`` connects as ``vagrant@localhost``
and is denied. The launch therefore passes ``-u mysql`` explicitly; combined
with the ``mysql`` OS user's socket peer-credential, ``auth_socket``
authenticates ``mysql@localhost`` with no password. ``--pager=cat`` makes the
interactive client dump results directly instead of paging through ``less``.

Pinned to its own ``xdist_group`` (see the design doc
``docs/superpowers/specs/2026-07-05-proxy-appshell-stability-tests-design.md``
§3.4) so repeated copies (``--count``) never contend for the same leased
mysql bed concurrently.

Runs via ``make stability-unix`` (``-m "stability and integration"``,
``--count=10``; nightly ``--count=100``); excluded from ``make coverage``
(``-m "not stability"``).
"""

import asyncio
import contextlib
import re
from collections.abc import Iterator

import pytest

from otto import AppShell, Parsed, register_login_proxy
from otto.storage.factory import create_host_from_dict
from tests._fixtures._host_pool import UNIX_POOL as _UNIX_POOL
from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.labdata import host_data

pytestmark = [
    pytest.mark.timeout(180),
    # `stability` keeps this soak test out of `make coverage` (`-m "not
    # stability"`); it runs via `make stability-unix` (tier 2). The bed
    # AppShell soak is pinned to its own xdist group so repeated `--count`
    # copies never share the leased mysql host concurrently.
    pytest.mark.stability,
    pytest.mark.xdist_group("app_shell_stability"),
]


# ---------------------------------------------------------------------------
# Module-scope login-proxy registration (containment: see module docstring).
#
# No per-proxy resync here — the post-transition resync lives in the shared
# engine (``otto.host.login_proxy._resync_shell``, called from the end of
# ``run_proxy``/``run_undo``) and applies to every hop automatically. See
# ``tests/e2e/host/test_login_proxy_e2e.py``'s module docstring NOTE.
# ---------------------------------------------------------------------------


async def _sudo_su_shell(io, ctx):
    # Root-mediated: non-root `su -s` is silently ignored for restricted-shell
    # targets (util-linux). vagrant is passwordless sudo on the test VMs. The
    # post-transition resync lives in the shared engine, not here.
    await io.send(f"sudo su -s /bin/bash {ctx.target.login}\n")


async def _sudo_su_shell_undo(io, ctx):
    await io.send("exit\n")


register_login_proxy("sudo-su-shell", _sudo_su_shell, undo=_sudo_su_shell_undo, overwrite=True)


_MYSQL_CREDS: list[dict[str, str]] = [
    {"login": "vagrant", "password": "vagrant"},
    {"login": "mysql", "password": "Password1", "proxy": "sudo-su-shell", "via": "vagrant"},
]


def _mysql_host_dict(ip: str, element: str, **overrides: object) -> dict[str, object]:
    """Build an inline host dict carrying the mysql proxied cred (default user vagrant)."""
    data: dict[str, object] = {
        "ip": ip,
        "element": element,
        "board": "seed",
        "creds": [dict(c) for c in _MYSQL_CREDS],
    }
    data.update(overrides)
    return data


class MySql(AppShell):
    """The interactive ``mysql`` client on the ``otto_test`` DB, as an app shell.

    ``user='mysql'`` makes :meth:`otto.host.host.BaseHost.app_shell` switch the
    session to the ``mysql`` OS user (via the ``sudo-su-shell`` proxy) before
    launching the client. See the module docstring for why ``-u mysql`` is
    required on the launch line.
    """

    launch = "mysql -u mysql --pager=cat otto_test"
    prompt = re.compile(r"mysql> \Z")
    quit_cmd = "quit"
    user = "mysql"


class Row(Parsed):
    """One data row of a bordered ``mysql`` result: ``|  1 | Alice | Smith |``.

    The ``id`` group is anchored to digits, so this pattern never matches the
    header row (``| id | first_name | last_name |``) or the ``+---+`` borders.
    """

    pattern = re.compile(
        r"\|\s*(?P<id>\d+)\s*\|\s*(?P<first_name>[^|]*?)\s*\|\s*(?P<last_name>[^|]*?)\s*\|"
    )
    id: int
    first_name: str
    last_name: str


class QueryStats(Parsed):
    """The ``N rows in set (X.XX sec)`` footer of an interactive SELECT."""

    pattern = re.compile(r"(?P<count>\d+)\s+rows?\s+in\s+set")
    count: int


class Select(Parsed):
    r"""A whole interactive ``mysql`` SELECT result — data rows plus the footer.

    Real ``mysql --pager=cat`` output is a border, a HEADER row, another border,
    the DATA rows, a closing border, then ``N rows in set (X.XX sec)``. A naive
    "first contiguous pipe run" grabs the header alone; this pattern instead
    matches the contiguous pipe-block that is *immediately followed by a closing
    border and the stats line* — i.e. the DATA block — so ``rows`` never
    includes the header or the borders. ``rows`` is parsed as ``list[Row]`` and
    ``stats`` recursively as :class:`QueryStats`.
    """

    pattern = re.compile(
        r"(?P<rows>(?:^\|[^\n]*\|[ \t]*\r?\n)+)"  # contiguous data-row block
        r"\+[-+]+\+[ \t]*\r?\n"  # closing border (not captured)
        r"(?P<stats>\d+\s+rows?\s+in\s+set[^\r\n]*)",  # footer stats line
        re.MULTILINE,
    )
    rows: list[Row]
    stats: QueryStats


@pytest.fixture
def leased_host(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, str]]:
    """Lease one Unix host from the pool; yield ``(element, ip)`` (IP read-only)."""
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, _UNIX_POOL) as element:
        yield element, host_data(element)["ip"]


async def _assert_sshd_reachable(element: str, ip: str) -> None:
    """Fail LOUD (host-named) if sshd isn't reachable on :22 — never skip."""
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, 22), timeout=10)
    except (OSError, asyncio.TimeoutError) as exc:
        raise RuntimeError(
            f"{element}_seed ({ip}) unreachable on :22 — bed down? "
            f"(AppShell mysql stability test must fail loud on host-down, never skip): {exc!r}"
        ) from exc
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_mysql_appshell_cycle(leased_host: tuple[str, str]) -> None:
    """CREATE + INSERT + parsed SELECT through a proxied ``mysql`` app shell, repeatedly.

    Proxies to the ``mysql`` OS user, launches the interactive client on
    ``otto_test``, then via :meth:`~otto.AppShell.cmd`: drops any leftover table
    (idempotency), creates it, inserts three known rows, and SELECTs them back
    parsed into a nested :class:`Select`. Asserts the concrete row contents and
    the ``N rows in set`` count against the real bordered output, then drops the
    table again so a re-run (including the next ``--count`` iteration) is
    clean. A SQL error surfaces as ``ERROR ...`` in the (un-parsed) command
    output — asserted absent — since an un-parsed ``cmd`` only fails on a
    prompt timeout, not on mysql's own error text.

    After the ``async with`` block exits, a plain ``host.run("echo back")`` on
    the host's SEPARATE default session proves ``_recover_session``-on-exit
    left that session usable — the soak invariant this file exists to flush
    flakiness out of: repeated under ``--count``, any intermittent failure to
    recover would surface here rather than hiding behind a single lucky pass.
    """
    element, ip = leased_host
    await _assert_sshd_reachable(element, ip)

    host = create_host_from_dict(_mysql_host_dict(ip, element))  # default user: vagrant
    try:
        async with host.app_shell(MySql) as sql:
            # Literal table name inline (no interpolation) — the table is a fixed
            # test fixture, and an f-string here would trip ruff's SQL-injection
            # lint (S608) for no real benefit.
            drop = await sql.cmd("DROP TABLE IF EXISTS otto_appshell_people;")
            assert "ERROR" not in drop.output, drop.output

            create = await sql.cmd(
                "CREATE TABLE otto_appshell_people "
                "(id INT PRIMARY KEY, first_name VARCHAR(32), last_name VARCHAR(32));"
            )
            assert "ERROR" not in create.output, create.output
            assert "Query OK" in create.output, create.output

            insert = await sql.cmd(
                "INSERT INTO otto_appshell_people VALUES "
                "(1,'Alice','Smith'),(2,'Bob','Jones'),(3,'Carol','Nguyen');"
            )
            assert "ERROR" not in insert.output, insert.output
            assert "3 rows affected" in insert.output, insert.output

            selected = await sql.cmd(
                "SELECT id, first_name, last_name FROM otto_appshell_people ORDER BY id;",
                parse=Select,
            )
            assert selected, f"SELECT parse failed: {selected.msg} (output={selected.output!r})"
            result: Select = selected.value
            assert result.stats.count == 3
            assert [(r.id, r.first_name, r.last_name) for r in result.rows] == [
                (1, "Alice", "Smith"),
                (2, "Bob", "Jones"),
                (3, "Carol", "Nguyen"),
            ]

            # Leave the bed clean so the next `--count` iteration (or a second
            # standalone run) starts from a known state.
            cleanup = await sql.cmd("DROP TABLE IF EXISTS otto_appshell_people;")
            assert "ERROR" not in cleanup.output, cleanup.output

        # The app shell's dedicated session is gone once the block exits;
        # `host.run` uses the host's SEPARATE default session, so its success
        # proves `_recover_session`-on-exit left the host uncorrupted.
        assert (await host.run("echo back")).only.value.strip() == "back"
    finally:
        await host.close()
