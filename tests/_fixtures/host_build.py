"""Build a host the supported way: the pipeline, then the readiness record it validates."""

from typing import Any

from otto.host import Element, RemoteHost, host_context
from otto.host.os_profile import ProfileContext


def build_host(
    entry: dict[str, Any],
    *,
    element: Element | None = None,
    selections: dict[str, list[str]] | None = None,
    preferences: dict[str, dict[str, Any]] | None = None,
    profiles: ProfileContext | None = None,
) -> RemoteHost:
    """Validate *entry* in its context and build it.

    *selections* is shorthand for capability preferences matching every host
    (``{".*": selections}``); pass *preferences* for the full selector-keyed
    table. Not both.
    """
    if selections is not None:
        if preferences is not None:
            raise TypeError("pass selections or preferences, not both")
        preferences = {".*": dict(selections)}
    hc = host_context(
        entry,
        element=element if element is not None else Element("lab"),
        profiles=profiles,
        preferences=preferences,
    )
    return hc.spec.to_host(ready=hc.spec.validate_in(hc.context))
