"""Unit tests for ``ConnectionManager``'s credential-resolution surface.

``credentials`` / ``login_target`` / ``proxy_hops`` are the seam that lets a
proxied login (e.g. ``su``-only ``mysql``) authenticate the transport as its
directly-loginable ``via`` account while still tracking the *requested*
login. See :mod:`otto.host.login_proxy` for the chain-resolution semantics.
"""

from otto.host.connections import ConnectionManager
from otto.host.login_proxy import Cred

ADMIN = Cred(login="admin", password="hunter2")
MYSQL = Cred(login="mysql", password="sqlpw", proxy="su", via="admin")


def _mgr(creds, user=None):
    return ConnectionManager(ip="10.0.0.1", creds=creds, user=user, term="ssh", name="h1")


def test_credentials_resolves_direct_cred_for_proxied_target():
    mgr = _mgr([MYSQL, ADMIN], user="mysql")
    assert mgr.credentials == ("admin", "hunter2")
    assert mgr.login_target == "mysql"
    assert [c.login for c in mgr.proxy_hops] == ["mysql"]


def test_credentials_plain_first_entry_default():
    mgr = _mgr([ADMIN, MYSQL])
    assert mgr.credentials == ("admin", "hunter2")
    assert mgr.login_target == "admin"
    assert mgr.proxy_hops == []


def test_credentials_empty_creds_loginless():
    mgr = _mgr([])
    assert mgr.credentials == ("", "")
    assert mgr.login_target == ""
