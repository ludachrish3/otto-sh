"""Every host family accepts every keyword the ``Host`` protocol's transfer verbs declare.

The protocol is the contract a family-agnostic caller trusts:
``otto.coverage.fetcher.remote`` calls ``host.get(files, dest, show_progress=False)``
through it, and until 2026-09-03 that raised ``TypeError`` on containers alone —
``DockerContainerHost.get`` had no ``show_progress`` while unix/local/embedded
did. Signature non-uniformity is invisible to every per-family test (each
calls its own class with the keywords that class happens to take), so this
holds the four concrete signatures against the protocol's in one place: a
keyword the protocol declares that a family does not accept is a bug on that
family, whatever the family does with it (containers forward ``show_progress``
to their staging leg, the way every family accepts ``user`` and does something
family-specific with it).

Superset, not equality: a family may take MORE (``Annotated`` CLI metadata,
``Exclude``d internals); it may not take less.
"""

import inspect

import pytest

from otto.host.docker_host import DockerContainerHost
from otto.host.embedded_host import EmbeddedHost
from otto.host.host import Host
from otto.host.local_host import LocalHost
from otto.host.unix_host import UnixHost

FAMILIES = [UnixHost, LocalHost, EmbeddedHost, DockerContainerHost]
TRANSFER_VERBS = ["get", "put"]


def _keywords(fn) -> set[str]:
    return {
        name
        for name, p in inspect.signature(fn).parameters.items()
        if name != "self" and p.kind is not inspect.Parameter.VAR_KEYWORD
    }


@pytest.mark.parametrize("verb", TRANSFER_VERBS)
@pytest.mark.parametrize("family", FAMILIES, ids=lambda c: c.__name__)
def test_every_family_accepts_every_protocol_keyword(family, verb):
    protocol = _keywords(getattr(Host, verb))
    concrete = _keywords(getattr(family, verb))
    missing = protocol - concrete
    assert not missing, (
        f"{family.__name__}.{verb} does not accept {sorted(missing)} — a caller going "
        f"through the Host protocol raises TypeError on this family alone"
    )


@pytest.mark.parametrize("verb", TRANSFER_VERBS)
def test_the_protocol_declares_show_progress(verb):
    """The keyword the fetcher relies on IS on the protocol — the parity test
    above would be vacuously green for it otherwise."""
    assert "show_progress" in _keywords(getattr(Host, verb))
