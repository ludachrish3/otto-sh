"""End-to-end tests for the :class:`otto.AppShell` REPL abstraction.

Two application REPLs are driven for real, end to end:

- ``python3 -u -i`` on the local machine (:class:`~otto.host.local_host.LocalHost`,
  no bed) — proves the whole AppShell lifecycle (launch, prompt-framed
  :meth:`~otto.AppShell.cmd`, typed :class:`~otto.Parsed` result, clean quit +
  POSIX-shell recovery) works with zero network transport, and that the
  session's sentinel-framed ``run`` is locked out while a shell is attached
  (``test_run_blocked_while_attached``);
- ``mysql`` on the live mysql-provisioned Unix bed, reached only through a
  root-mediated ``sudo su -s /bin/bash mysql`` login proxy (the same proxy the
  login-proxy e2e uses) — the full story: proxy in, launch the client, CREATE a
  table, INSERT rows, and SELECT them back parsed into a nested
  ``Select``/``Row``/``QueryStats`` object graph authored against the *real*
  bordered ``mysql`` table output.

Containment (mirrors ``test_login_proxy_e2e.py``)
-------------------------------------------------
``CredSpec`` validates a cred's ``proxy`` against the ``LOGIN_PROXIES`` registry
at ingest, so a proxy-referencing cred must never be written to shared lab data
(that would break every unit context that loads it without the proxy
registered). This module is therefore self-contained: it registers the
``sudo-su-shell`` proxy at module scope (``overwrite=True`` so re-import under
xdist and co-import with the login-proxy e2e are both idempotent) and builds its
mysql host from an INLINE dict, reading only the leased VM's IP read-only from
``tech1/hosts.json`` — never its creds. Zero shared-file mutation.

mysql connection note
---------------------
The ``mysql`` client resolves its *default* DB user from the terminal login name
(``getlogin()`` / utmp = the SSH login, ``vagrant``), NOT from the ``su``'d euid
(``mysql``), so a bare ``mysql`` connects as ``vagrant@localhost`` and is
denied. The launch therefore passes ``-u mysql`` explicitly; combined with the
``mysql`` OS user's socket peer-credential, ``auth_socket`` authenticates
``mysql@localhost`` with no password. ``--pager=cat`` makes the interactive
client dump results directly instead of paging through ``less``.
"""

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

from otto import AppShell, Parsed, register_login_proxy
from otto.host.app_shell import AppShellActiveError
from otto.host.local_host import LocalHost
from otto.storage.factory import create_host_from_dict
from tests._fixtures._host_pool import UNIX_POOL as _UNIX_POOL
from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.labdata import host_data

# ---------------------------------------------------------------------------
# python3 REPL — runs hostless on LocalHost (no bed)
# ---------------------------------------------------------------------------


class PyRepl(AppShell):
    """The stock CPython interactive interpreter as an :class:`otto.AppShell`."""

    launch = "python3 -u -i"
    prompt = re.compile(r">>> \Z")
    quit_cmd = "exit()"


class Version(Parsed):
    """``major.minor`` extracted from a ``sys.version_info`` print."""

    pattern = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)")
    major: int
    minor: int


@pytest_asyncio.fixture
async def local_host() -> AsyncIterator[LocalHost]:
    """A :class:`~otto.host.local_host.LocalHost`, closed on teardown."""
    host = LocalHost()
    try:
        yield host
    finally:
        await host.close()


@pytest.mark.hostless
@pytest.mark.asyncio
async def test_python_repl_roundtrip(local_host: LocalHost) -> None:
    """Drive python3 through an app shell, parse a typed result, and recover.

    Launches ``python3 -u -i`` on the local machine, runs one line that prints
    the interpreter's ``major.minor`` version, and parses it into a typed
    :class:`Version`. After the shell exits, the host's ordinary
    sentinel-framed ``run`` must work again — proving the POSIX shell was
    cleanly recovered.
    """
    async with local_host.app_shell(PyRepl) as py:
        result = await py.cmd(
            "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')",
            parse=Version,
        )
        assert result, f"parse failed: {result.msg} (output={result.output!r})"
        assert result.value.major == 3

    # The session is back to a working POSIX shell after the app shell exits.
    assert (await local_host.run("echo back")).only.value.strip() == "back"


@pytest.mark.hostless
@pytest.mark.asyncio
async def test_run_blocked_while_attached(local_host: LocalHost) -> None:
    """A session's sentinel-framed ``run`` is locked out while a shell is attached.

    ``app_shell`` isolates the REPL on its own dedicated session; the lock lives
    on *that* session, so this test attaches directly to an explicitly-opened
    session (:meth:`AppShell.attach`, the documented public API) and asserts
    that session's ``run`` raises :class:`AppShellActiveError` — the command
    frame must never be typed into the app. Once the shell exits, the lock is
    gone and the session is usable again.
    """
    session = await local_host.open_session("appshell_lock_probe")
    try:
        async with PyRepl.attach(session):
            with pytest.raises(AppShellActiveError):
                await session.run("echo nope")
        # Lock released on exit: the same session runs normally again.
        assert (await session.run("echo ok")).only.value.strip() == "ok"
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# mysql full story — runs on the live mysql-provisioned Unix bed
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
            f"(AppShell mysql e2e must fail loud on host-down, never skip): {exc!r}"
        ) from exc
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


@pytest.mark.integration
@pytest.mark.xdist_group("app_shell_e2e")
@pytest.mark.asyncio
async def test_mysql_appshell_full_story(leased_host: tuple[str, str]) -> None:
    """CREATE + INSERT + parsed SELECT through a proxied ``mysql`` app shell.

    Proxies to the ``mysql`` OS user, launches the interactive client on
    ``otto_test``, then via :meth:`~otto.AppShell.cmd`: drops any leftover table
    (idempotency), creates it, inserts three known rows, and SELECTs them back
    parsed into a nested :class:`Select`. Asserts the concrete row contents and
    the ``N rows in set`` count against the real bordered output, then drops the
    table again so a re-run is clean. A SQL error surfaces as ``ERROR ...`` in
    the (un-parsed) command output — asserted absent — since an un-parsed
    ``cmd`` only fails on a prompt timeout, not on mysql's own error text.
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

            # Leave the bed clean so a second run starts from a known state.
            cleanup = await sql.cmd("DROP TABLE IF EXISTS otto_appshell_people;")
            assert "ERROR" not in cleanup.output, cleanup.output
    finally:
        await host.close()
