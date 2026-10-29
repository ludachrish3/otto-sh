"""Who a login is, as far as elevation is concerned: the one root rule.

A leaf with no imports, so every package that asks "does this session already
act as root" -- the elevation seam, ``su``'s challenge rule, the check and
link probes, the kmod plan -- imports the same predicate at module top
without pulling in the session machinery.
"""

ROOT_LOGIN = "root"
"""The login that is root, and the one a bare, argument-less ``su`` targets."""


def is_root(login: str) -> bool:
    """Whether *login* is root, positively named.

    An empty login is an identity otto never established, and is NOT read as
    root: reading it as root would skip an elevation (or an ``su`` password)
    that is in fact needed (see ``otto.host.login_proxy._may_challenge``).
    """
    return login == ROOT_LOGIN
