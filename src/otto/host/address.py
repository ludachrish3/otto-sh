"""The one rule for what an address may be.

An address reaches otto from a lab file, an inventory record or a NetBox
custom field, and from there it is spliced into commands a host runs. The
model boundary refuses anything that is not an IP address or, where a name
is allowed, a strict DNS hostname, so a value such as
``1.2.3.4'; touch /tmp/x; '`` never gets as far as a command. The command
builders quote what they splice in as well (:mod:`otto.host.shell_quote`).

The module is standard-library only, so a probe builder can apply the rule
without importing the host specs.
"""

import re
from ipaddress import IPv6Address, ip_address

_HOSTNAME_MAX = 253
"""Longest DNS name, in characters, without the trailing dot."""

_LABEL = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?")
"""One DNS label: letters, digits, ``_`` and inner hyphens, 1 to 63 characters.

RFC 1123 has no ``_``, but lab and Active Directory names use it, and it is
inert to a shell and to socat."""

_INTERFACE_NAME = re.compile(r"[A-Za-z0-9_.:@][A-Za-z0-9_.:@-]{0,14}")
"""A Linux netdev name: at most 15 characters (``IFNAMSIZ`` less its NUL).

The kernel refuses ``/``, whitespace, ``.`` and ``..``; this is stricter, the
characters real interface names use (``eth0``, ``enp0s3``, ``br-lan``,
``eth0.100``, ``eth0:1``, ``veth@if5``), with no leading ``-`` so a name can
never read as an option."""


def validate_interface_name(value: str) -> str:
    """Return *value* if it is a netdev name, as an IPv6 scope ID must be; else raise ValueError.

    Only the scope ID is held to it. An ``interfaces`` key is not: NetBox and
    network gear name ports such as ``GigabitEthernet0/1``, so the commands
    that use a key quote it instead.

    >>> from otto.host.address import validate_interface_name
    >>> validate_interface_name("eth0.100")
    'eth0.100'
    """
    if value in {".", ".."} or not _INTERFACE_NAME.fullmatch(value):
        raise ValueError(
            f"interface name {value!r} is not a netdev name (1-15 of letters, digits, "
            "'_', '.', ':', '@', '-'; not '.' or '..', no leading '-')"
        )
    return value


def _is_hostname(value: str) -> bool:
    # One trailing dot is a fully qualified name's root label, and harmless.
    name = value.removesuffix(".")
    if not name or len(name) > _HOSTNAME_MAX:
        return False
    labels = name.split(".")
    # An all-digit last label is a mistyped IPv4 address (``10.0.0.256``),
    # never a name: no top-level domain is numeric.
    return all(_LABEL.fullmatch(label) for label in labels) and not labels[-1].isdigit()


def validate_address(value: str, *, allow_hostname: bool) -> str:
    """Return *value* if it is an IP address, or a DNS hostname when *allow_hostname*.

    A hostname is dot-separated labels of letters, digits, ``_`` and inner
    hyphens, with at most one trailing dot. An IPv6 scope ID
    (``fe80::1%eth0``) must be an interface name
    (:func:`validate_interface_name`); ``ipaddress`` alone accepts any text
    after ``%``, including ``;`` and spaces.
    Raises :class:`ValueError` naming the value otherwise; a pydantic field
    validator that calls this reports it under the field's name.

    >>> from otto.host.address import validate_address
    >>> validate_address("fe80::1%eth0", allow_hostname=False)
    'fe80::1%eth0'
    >>> validate_address("router1.lab", allow_hostname=True)
    'router1.lab'
    >>> validate_address("router1.lab", allow_hostname=False)
    Traceback (most recent call last):
    ...
    ValueError: address 'router1.lab' is not a valid IP address
    """
    try:
        parsed = ip_address(value)
    except ValueError:
        parsed = None
    if parsed is not None:
        scope = parsed.scope_id if isinstance(parsed, IPv6Address) else None
        if scope is not None:
            try:
                validate_interface_name(scope)
            except ValueError as exc:
                raise ValueError(f"address {value!r} has scope ID {scope!r}: {exc}") from None
        return value
    if allow_hostname and _is_hostname(value):
        return value
    what = "a valid IP address or DNS hostname" if allow_hostname else "a valid IP address"
    raise ValueError(f"address {value!r} is not {what}")
