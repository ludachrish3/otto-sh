r"""Reference :class:`~otto.Parsed` models for the AppShell parse engine (sample).

:meth:`~otto.host.app_shell.AppShell.cmd` turns a REPL's raw text answer into a
typed object: a :class:`~otto.Parsed` subclass pairs a pydantic model with the
compiled regex that produces it, and :func:`~otto.host.app_shell.parse_one` /
:func:`~otto.host.app_shell.apply_parse` are the functions ``cmd(parse=...)``
dispatches to under the hood. Both are pure text-in, object-out functions, so
they (and the models below) are exercised here against literal strings — no
host required.

A single-level model maps named groups straight to fields:

>>> from otto.examples.app_shell import Version
>>> from otto.host.app_shell import parse_one
>>> version = parse_one(Version, "Python 3.10.12")
>>> version.major, version.minor
(3, 10)

Composite REPL output — a bordered table *and* a trailing summary line, say —
nests: a field typed as another :class:`~otto.Parsed` subclass (or
``list[Sub]``) is parsed *recursively* from the region its own named group
captured, so pattern/model drift is impossible at any level (each subclass
self-checks its own group-to-field mapping at class-definition time):

>>> from otto.examples.app_shell import Listing, Row
>>> from otto.host.app_shell import apply_parse
>>> text = "| 1 | alice |\n| 2 | bob   |\n2 rows returned\n"
>>> listing = parse_one(Listing, text)
>>> [row.name for row in listing.table]
['alice', 'bob']
>>> listing.stats.count
2

``apply_parse`` is what ``cmd(parse=...)`` calls once it has the shell's raw
output in hand; passing a bare ``list[Model]`` spec is the same
``finditer``-per-row path ``Listing.table`` used above:

>>> apply_parse(list[Row], text) == listing.table
True

The REPL half of :class:`~otto.host.app_shell.AppShell` wraps an application
already running inside a shell session — mysql, a vendor CLI, ``python3``.
Driving one for real needs a live host, so it is illustrated rather than
exercised here:

.. code-block:: python

    import re
    from otto import AppShell


    class PyRepl(AppShell):
        launch = "python3 -u -i"
        prompt = re.compile(r">>> \Z")
        quit_cmd = "exit()"


    async with host.app_shell(PyRepl) as py:
        result = await py.cmd(
            "print(f'{sys.version_info[0]}.{sys.version_info[1]}')",
            parse=Version,
        )
        result.value.major  # 3

A proxied login (the login-proxy half of this feature — see
:mod:`otto.examples.login_proxy`) composes transparently: pass ``user=`` to
:meth:`~otto.host.host.BaseHost.app_shell` (or set ``AppShell.user`` on the
subclass) and the cred's proxy hops run before ``launch`` is sent. Reaching a
``mysql`` account this way has one real-world twist worth knowing up front:
once proxied to the OS ``mysql`` user via ``sudo su``, the client needs an
*explicit* ``-u mysql`` —

.. code-block:: python

    class MySql(AppShell):
        launch = "mysql -u mysql --pager=cat otto_test"
        prompt = re.compile(r"mysql> \Z")
        quit_cmd = "quit"
        user = "mysql"

— because ``mysql`` otherwise resolves its default DB user from the
terminal's login name (the account that authenticated), not from the
proxied effective uid.
"""

import re

from otto.host.app_shell import Parsed


class Version(Parsed):
    """``major.minor`` extracted from a REPL's version banner or print."""

    pattern = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)")
    major: int
    minor: int


class Row(Parsed):
    """One bordered table row, e.g. ``| 1 | alice |``."""

    pattern = re.compile(r"^\|\s*(?P<id>\d+)\s*\|\s*(?P<name>\S+)\s*\|\s*$", re.MULTILINE)
    id: int
    name: str


class Stats(Parsed):
    """A trailing row-count summary line, e.g. ``2 rows returned``."""

    pattern = re.compile(r"(?P<count>\d+) rows? returned")
    count: int


class Listing(Parsed):
    """A composite REPL answer: the bordered table *and* its summary line.

    ``table`` and ``stats`` each recurse into their own nested model from the
    region the outer pattern's same-named group captured — ``table`` via
    ``list[Row]`` (one :class:`Row` per match), ``stats`` via the single
    nested :class:`Stats` model.
    """

    pattern = re.compile(
        r"(?P<table>(?:^\|.+\|$\n?)+)\s*(?P<stats>\d+ rows? returned)",
        re.MULTILINE,
    )
    table: list[Row]
    stats: Stats
