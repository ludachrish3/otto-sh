"""Programmatic products: one ``agent`` per version the host's metadata lists.

The page under docs/cookbook/extending/product-providers.md includes this
file. It is not in the example project's ``init`` list — the worked example
defines ``agent`` in data, and a name is defined in data OR in code.
"""

# doc: begin provider
from pathlib import Path

from otto.context import variant
from otto.host import DeclaredProduct
from otto.host.host import Host
from otto.host.product import Product, register_product_provider

# The project root (this file is libs/gs_example/versions.py). A provider anchors
# its own paths; an entry's `artifact` is anchored for it.
ROOT = Path(__file__).resolve().parents[2]


def agents_for(host: Host) -> list[Product] | None:
    """One product per version the host's metadata asks for, built for the run's variant."""
    if host.os_type != "unix":
        return None
    suffix = "-field" if variant() == "field" else "-debug"
    return [
        DeclaredProduct(
            name=f"agent-{version}",
            artifact=ROOT / f"build/agent-{version}{suffix}",
            stage_dir=Path(f"/opt/agent-{version}"),
            install_cmd=f"/opt/agent-{version}/agent-{version}{suffix} --install",
            check_cmd=f"test -x /opt/agent-{version}/agent-{version}{suffix}",
        )
        for version in host.element.metadata.get("agent_versions", [])
    ]


register_product_provider(agents_for)
# doc: end provider
