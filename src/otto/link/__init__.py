"""The link subsystem: the unified ``Link`` edge model and its derivations.

Foundation (sub-project #1): model, static derivation, sentinel codec, and
the discovery *contract*. Live tunnel creation/discovery arrives with the
``otto link`` CLI (sub-project #2).
"""

from .discovery import all_links, discover_dynamic_links, discover_dynamic_links_status
from .manage import AddedTunnel, RemovedReport, add_link, remove_all_links, remove_link
from .model import Link, LinkEndpoint, Provenance, make_link_id

__all__ = [
    "AddedTunnel",
    "Link",
    "LinkEndpoint",
    "Provenance",
    "RemovedReport",
    "add_link",
    "all_links",
    "discover_dynamic_links",
    "discover_dynamic_links_status",
    "make_link_id",
    "remove_all_links",
    "remove_link",
]
