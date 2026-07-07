"""Pydantic boundary specs for a ``lab.json`` ``links`` entry.

Structural validation only: endpoint *references* (host ids, interface keys)
are resolved against the loaded host set at lab-load time
(:func:`otto.link.derive.resolve_declared_links`), where the hosts are known.
"""

from pydantic import Field, field_validator, model_validator

from .base import OttoModel


class LinkEndpointSpec(OttoModel):
    """One end of a declared link: a host id plus (optionally) a named interface.

    ``interface`` (a key in the host's ``interfaces`` map, i.e. a netdev name)
    is required only when the host defines more than one interface; with one or
    none, otto assumes the sole interface / the management ``ip``.
    """

    host: str
    interface: str | None = None


class LinkSpec(OttoModel):
    """Boundary spec for one ``links`` entry in ``lab.json``.

    ``protocol`` is informational for declared links (what the route carries);
    it becomes functional for dynamic links (sub-project #2). ``impair`` and
    ``management`` are reserved for sub-projects #3/#5: accepted and carried,
    not yet consumed.
    """

    endpoints: list[LinkEndpointSpec] = Field(min_length=2, max_length=2)
    protocol: str = "tcp"
    name: str | None = None
    impair: str | None = None
    management: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _strip_comment_keys(cls, data: object) -> object:
        """Drop ``_``-prefixed keys — the JSON comment idiom (see HostSpec)."""
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not (isinstance(k, str) and k.startswith("_"))}
        return data

    @field_validator("protocol")
    @classmethod
    def _normalize_protocol(cls, v: str) -> str:
        return v.lower()

    @model_validator(mode="after")
    def _distinct_endpoints(self) -> "LinkSpec":
        a, b = self.endpoints
        if a.host == b.host and a.interface == b.interface:
            raise ValueError("link endpoints must differ (same host and interface on both ends)")
        return self
