"""The link subsystem: the unified ``Link`` edge model and its derivations.

Model, static derivation, and id computation for the static link layer
(implicit hop edges, declared routes). Live tunnel creation/discovery lives
in ``otto.tunnel``.
"""

from .model import Link, LinkEndpoint, Provenance, make_link_id, make_static_link_id

__all__ = [
    "Link",
    "LinkEndpoint",
    "Provenance",
    "make_link_id",
    "make_static_link_id",
]
