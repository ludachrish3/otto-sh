"""Creds-store errors (spec 2026-09-06 creds-store §5.1)."""

from ..errors import OttoError


class CredsError(OttoError):
    """A creds store could not answer: I/O, parse, network, auth, or a bad entry."""


class CredsConstructionError(CredsError, ValueError):
    """A ``[creds]`` table could not be prepared or built.

    Raised for every stage of turning the table into a store: its backend is
    not registered (lookup), its keys do not parse (parse), the store's
    factory failed (construction), or what it built is not a creds store
    (result). The message names the stage, the backend, the module that
    registered it and the settings file that declared the table. A
    ``ValueError`` as well, because each of these is a configuration mistake;
    a store's failures while answering stay plain :class:`CredsError`.
    """
