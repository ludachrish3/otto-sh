"""The one place otto imports asyncssh.

``import asyncssh`` looks for two optional native libraries while it loads:
liboqs (the sntrup761 key exchange) and nettle (the UMAC message
authentication codes). It asks ``ctypes.util.find_library``, which on glibc
Linux has three legs: the dynamic linker's cache (``ldconfig -p``), then
``gcc``, then ``ld``. The last two start a compiler and a linker to answer a
name the cache does not list. A machine without liboqs paid that on every
command that reached SSH: eight extra processes and about a thousand file
operations, before the connection opened.

This module imports asyncssh with the lookup held to the first leg. Every other
module takes the name from here::

    from ._asyncssh import asyncssh

A function that needs asyncssh writes that line in its own body, as it wrote
``import asyncssh`` before: the import stays as late as it was. A name used only
in annotations is still imported from ``asyncssh`` itself under
``if TYPE_CHECKING:``, which loads nothing.

**What this gives up.** A liboqs or nettle the linker cache does not list is
not found, even where ``gcc`` or ``ld`` would have found it: a library
installed without running ``ldconfig``, or one reachable only through
``LIBRARY_PATH`` or ``LD_LIBRARY_PATH``. On a system whose C library has no
``ldconfig -p`` (musl), neither library is found. asyncssh then offers no
sntrup761 key exchange or no UMAC, and negotiates the algorithms it still has.
:doc:`/architecture/ssh-algorithms` states this for users.

The lookup is held for the whole of asyncssh's import, so a library one of its
own dependencies looks up in that time is answered from the cache as well.

asyncssh imported by other code before otto reaches this module has already run
its own lookup; this import then finds it loaded and changes nothing.
"""

import contextlib
import ctypes.util
from collections.abc import Generator

__all__ = ["asyncssh"]


@contextlib.contextmanager
def _linker_cache_lookup() -> Generator[None, None, None]:
    """Hold ``ctypes.util.find_library`` to the dynamic linker's cache.

    The cache leg is the stdlib's own function, so what it finds and how it
    reads ``ldconfig`` stay the stdlib's. Only the glibc branch of
    ``ctypes.util`` defines it. Where it is absent (macOS, the BSDs, or a
    CPython that renamed it) the lookup is left as the platform has it; on
    Linux the SSH surfaces of the import budget would then show the compiler
    runs coming back.
    """
    cache_leg = getattr(ctypes.util, "_findSoname_ldconfig", None)
    if cache_leg is None:
        yield
        return
    platform_lookup = ctypes.util.find_library
    ctypes.util.find_library = cache_leg
    try:
        yield
    finally:
        ctypes.util.find_library = platform_lookup


with _linker_cache_lookup():
    import asyncssh
