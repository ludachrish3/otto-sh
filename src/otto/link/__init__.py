"""The link subsystem: the unified ``Link`` edge model and its derivations.

Foundation (sub-project #1): model, static derivation, sentinel codec, and
the discovery *contract*. Live tunnel creation/discovery arrives with the
``otto link`` CLI (sub-project #2).
"""

from .model import Link, LinkEndpoint, Provenance, make_link_id

__all__ = ["Link", "LinkEndpoint", "Provenance", "make_link_id"]
