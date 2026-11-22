"""Quoting host data into the commands otto runs on a host.

A probe or dial command is one string handed to the host's shell, and the
values spliced into it (an address, a port, a bind address) come from lab
data. Each value is quoted for the syntax it lands in:

- a shell word: :func:`shell_operand` (a remote path: :func:`path_operand`),
  or a positional parameter of :func:`bash_script`, so the script text never
  contains the value at all;
- a socat address: :func:`socat_host`, which takes an address or hostname only;
- a template a user wrote, which may re-parse the value in a second shell
  (``ssh hv virsh start {name}``): the value must be inert as it stands
  (:func:`shell_active_char`), since no quoting survives every nesting depth;
- Python source: a ``repr()`` literal, at the builder that writes the source.

Addresses are validated at the model boundary too
(:func:`otto.host.address.validate_address`); quoting here is the second
line, for a value that reached a builder some other way.
"""

import re
import shlex
from pathlib import PurePath

from .address import validate_address


def bash_script(script: str, *args: str) -> str:
    """Run *script* under bash, with *args* as its positional parameters ``$1``, ``$2``, ….

    The script is one quoted word and each argument another, after a ``_``
    that fills ``$0``, so a value passed in *args* is data to the script and
    can never become script text. A caller often runs this under ``sh -c``
    (dash or BusyBox ash on many hosts); the ``bash -c`` is what lets the
    script use bash-only features such as ``$EPOCHREALTIME`` or
    ``/dev/tcp``.

    >>> from otto.host.shell_quote import bash_script
    >>> print(bash_script('echo "$1"', "a b; c"))
    bash -c 'echo "$1"' _ 'a b; c'
    """
    words = [shlex.quote(script)]
    if args:
        words += ["_", *(shlex.quote(arg) for arg in args)]
    return "bash -c " + " ".join(words)


def shell_operand(value: str) -> str:
    """Quote *value* as one shell word that a program reads as an operand, never an option.

    Quoting keeps the shell from splitting or expanding it; a leading ``-``
    would still reach the program as an option (``nc -e``, ``ping -f``), so
    it is refused. No address or hostname begins with one.
    """
    if value.startswith("-"):
        raise ValueError(f"{value!r} begins with '-' and would be read as an option")
    return shlex.quote(value)


def path_operand(path: "PurePath | str") -> str:
    """Quote a remote *path* as one shell word that a program reads as a path, never an option.

    The one quoter for a path in a command otto runs on a host, over
    :func:`shell_operand`. A path is never refused: a relative path that
    begins with ``-`` (a relative destination joined to a file named ``-rf``)
    is spelled ``./-rf``, the same file, which no program reads as an option.

    >>> from otto.host.shell_quote import path_operand
    >>> print(path_operand("/tmp/a b"), path_operand("-rf"))
    '/tmp/a b' ./-rf
    """
    text = str(path)
    return shell_operand(f"./{text}" if text.startswith("-") else text)


def rm_f_command(path: "PurePath | str") -> str:
    """Return ``rm -f <path>``: remove a file, and succeed when it is already gone.

    The one spelling of "remove this file" for a path otto staged or placed
    (a toolchain tool, a staged tarball or ``.ko``, the file cleanup removes),
    quoted by :func:`path_operand`.

    >>> from otto.host.shell_quote import rm_f_command
    >>> print(rm_f_command("/opt/o t/app.bin"))
    rm -f '/opt/o t/app.bin'
    """
    return f"rm -f {path_operand(path)}"


def rmdir_if_empty(*paths: "PurePath | str", skip_sticky: bool = False) -> str:
    """Return the command that removes each of *paths* that is an empty directory; it never fails.

    ``rmdir`` refuses a directory that is absent, not empty, or that the
    session cannot remove, and each refusal is silent here and leaves the
    status 0: a directory kept for any of those reasons is not an error. The
    paths are tried in the order given (a caller lists a child before its
    parent) and the command is one brace group, so it can follow ``&&`` or
    stand on a line of its own.

    With *skip_sticky* a directory whose sticky bit is set (``test -k``:
    ``/var/tmp``, ``/dev/shm``) is not tried either: it is a shared scratch
    directory, and recreating it plain would lose the bit.

    >>> from otto.host.shell_quote import rmdir_if_empty
    >>> print(rmdir_if_empty("/a/b", "/a"))
    { rmdir /a/b /a 2>/dev/null || true; }
    >>> print(rmdir_if_empty("/a/b", skip_sticky=True))
    { for d in /a/b; do test -k "$d" || rmdir "$d" 2>/dev/null; done; true; }
    """
    operands = " ".join(path_operand(p) for p in paths)
    if skip_sticky:
        return f'{{ for d in {operands}; do test -k "$d" || rmdir "$d" 2>/dev/null; done; true; }}'
    return f"{{ rmdir {operands} 2>/dev/null || true; }}"


_SHELL_ACTIVE = re.compile(r"[^A-Za-z0-9_.:@%+=,/-]")
"""Any character outside the shell-inert set: letters, digits and ``_.:@%+=,/-``."""


def shell_active_char(value: str) -> "str | None":
    """Return the first character of *value* a shell could act on, or ``None`` if it is inert.

    Inert means letters, digits and ``_.:@%+=,/-`` only: no quote, space,
    glob, expansion, separator or redirection, so the value means the same
    word however many shells parse it. That covers host names, IDs and IP
    addresses, IPv6 with a ``%scope`` included.

    >>> from otto.host.shell_quote import shell_active_char
    >>> shell_active_char("fe80::1%eth0"), shell_active_char("vm1; reboot")
    (None, ';')
    """
    found = _SHELL_ACTIVE.search(value)
    return None if found is None else found.group()


def socat_host(value: str) -> str:
    """Return *value* as the host part of a socat address (``TCP4:<host>:<port>``, ``bind=<host>``).

    The value must pass :func:`~otto.host.address.validate_address` (an IP
    address or a hostname), so it can hold none of socat's own syntax: no
    ``,`` to begin an option, no ``!!`` to begin a second address, no quote
    or bracket. An IPv6 literal, whose ``:`` would split the address, is
    returned in brackets. The caller still quotes the whole address for the
    shell.
    """
    validate_address(value, allow_hostname=True)
    return f"[{value}]" if ":" in value else value
