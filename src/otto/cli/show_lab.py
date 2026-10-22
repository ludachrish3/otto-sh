"""``otto --show-lab``: the loaded lab drawn as a tree in a rounded panel.

The data is the library's (:func:`otto.labs.overview.lab_overview`); this
module only draws it. The lab name is the root; each host is a branch
carrying its kind, with its address, transports and products beneath; the
links come last, one line each. Every cell is a :class:`rich.text.Text`, so a
bracket in a host id or a product name prints as written.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console
    from rich.tree import Tree

    from ..labs.loader import Lab
    from ..labs.overview import HostOverview, LabOverview


DETAIL_LEVEL = 2
"""The tree level holding a host's address, transports and products, and each link."""


def _host_branch(tree: "Tree", host: "HostOverview", *, details: bool) -> None:
    from rich.text import Text

    label = Text(host.id, style="bold cyan")
    label.append(f"  {host.kind}", style="dim")
    branch = tree.add(label)
    if not details:
        return
    for key, value in (
        ("address", host.address),
        ("transports", ", ".join(host.transports)),
        ("products", ", ".join(host.products)),
    ):
        if value:
            row = Text(f"{key}: ", style="dim")
            row.append(value)
            branch.add(row)


def lab_tree(overview: "LabOverview", *, depth: int = 0) -> "Tree":
    """Build the tree for *overview*; *depth* levels below the lab name open (0 = all).

    Level 1 is a host's id and kind (and the ``links`` heading, which carries
    the link count while closed); level 2 is a host's address, transports and
    products, and each link.
    """
    from rich.text import Text
    from rich.tree import Tree

    details = depth == 0 or depth >= DETAIL_LEVEL
    root = Tree(Text(f"lab {overview.name}", style="bold"), guide_style="dim")
    for host in overview.hosts:
        _host_branch(root, host, details=details)
    if overview.links:
        heading = "links" if details else f"links ({len(overview.links)})"
        links = root.add(Text(heading, style="bold"))
        if details:
            for link in overview.links:
                row = Text(link.id, style="bold cyan")
                row.append(f"  {link.a} <-> {link.b}  ")
                row.append(link.protocol, style="dim")
                links.add(row)
    return root


def render_lab(lab: "Lab", *, depth: int = 0, console: "Console | None" = None) -> None:
    """Print *lab* as a tree in a rounded panel, as ``otto --show-lab`` does."""
    from rich import box, get_console
    from rich.panel import Panel

    from ..labs.overview import lab_overview

    out = console if console is not None else get_console()
    out.print(Panel(lab_tree(lab_overview(lab), depth=depth), box=box.ROUNDED, expand=False))
