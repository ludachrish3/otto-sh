"""
Abstract base for network-reached hosts.

``RemoteHost`` is the common ancestor of every host class that talks to a
target across a network — :class:`UnixHost` (SSH/Telnet to a bash shell),
:class:`EmbeddedHost` (telnet to an RTOS shell), and any future siblings such
as a Windows-host class. It is deliberately distinct from :class:`LocalHost`,
which runs commands on the local machine and shares no network plumbing.

History: this name used to belong to the *concrete* SSH/Telnet bash host.
That class is now :class:`UnixHost`; ``RemoteHost`` is the abstract parent.
The split makes the OS family of a host explicit (lab data carries an
``osType`` field) and gives embedded targets a place to live alongside Unix
ones without lying about their shape.

Phase 2 of the embedded-host feature introduces the split with the abstract
class kept intentionally thin — it only marks the category. As subsequent
phases land, network identity (``ip``, ``ne``, ``hop``, naming) and the
``SshHopTransport`` machinery currently in :class:`UnixHost` will be lifted
up here so :class:`EmbeddedHost` can share them without duplication.
"""

from .host import BaseHost


class RemoteHost(BaseHost):
    """Abstract base class for any host reached over a network.

    Concrete subclasses (:class:`UnixHost`, :class:`EmbeddedHost`) supply the
    transport-specific session/transfer machinery. Do not instantiate this
    class directly.
    """

    # Keep slots harmony with the concrete dataclass subclasses, whose
    # ``@dataclass(slots=True)`` would otherwise produce instances that mix
    # ``__slots__`` with the inherited ``__dict__`` from this base.
    __slots__ = ()
