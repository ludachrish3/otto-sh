#!/usr/bin/env python3
"""The scripts' one read of a bed cell's lab file, and the addresses ``lab-reachability`` pings.

A bed cell's lab file (the Makefile's ``LAB_FILE``) is ``tech1/lab.jsonnet``:
a Jsonnet entry, never read as raw JSON. Every script that reads it (the bed
lease, the bed reap, the lab health check) reads it here, through otto's one
lab-document reader (:func:`otto.labs.documents.read_lab_document`), so a
script sees exactly the document otto loads:

- :func:`read_cell_document` returns the file's validated sections. A
  ``.jsonnet`` cell is rendered as ``load_lab(search_paths=[cell])`` renders
  it (:func:`otto.labs.loader.prepare_source_for_paths`), under
  :func:`otto.labs.loader.search_paths_view`, ``load_lab``'s own view for
  it: no inventory, and the user settings file's protected-file catalog,
  which refuses when that file cannot be read. A ``.json`` file is read as
  it is.
- :func:`load_cell_lab` loads the cell's labs as ``otto.load_lab`` does.

Usage::

    uv run python scripts/_lab_document.py reach <lab file>

prints one management address per line (each host entry's ``ip``) and never
the document, so ``make lab-reachability`` pings the bed without going
through ``otto lab render``, which refuses tech1's inline passwords.
"""

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from otto.labs.documents import RenderInputs
    from otto.labs.loader import Lab


def cell_render_inputs(path: Path) -> "RenderInputs | None":
    """Return what the cell *path* renders with, or ``None`` for a file read as it is.

    A ``.jsonnet`` cell is the entry of the ``jsonnet`` source
    ``load_lab(search_paths=[path])`` builds, rendered under the view
    ``load_lab`` renders it under (:func:`otto.labs.loader.search_paths_view`).

    Raises:
        LabRepositoryError: The source has no entry file at *path*.
    """
    from otto.labs.jsonnet_source import JsonnetSourceInputs
    from otto.labs.loader import prepare_source_for_paths, search_paths_view

    inputs = JsonnetSourceInputs.of_prepared(prepare_source_for_paths([path]))
    return None if inputs is None else inputs.render_inputs(search_paths_view())


def read_cell_document(path: Path) -> dict[str, Any]:
    """Return the validated ``labs``/``elements``/``links`` sections of the cell's lab file *path*.

    Read by :func:`otto.labs.documents.read_lab_document`, the one reader,
    with :func:`cell_render_inputs`: secret values are wrapped
    (``SecretStr``), as every otto reader leaves them.

    Raises:
        LabRepositoryError: Naming *path*, when it cannot be read, rendered
            or parsed.
    """
    from otto.labs.documents import read_lab_document

    return read_lab_document(path, render=cell_render_inputs(path)).sections


def load_cell_lab(path: Path, labnames: "str | list[str]", **kwargs: Any) -> "Lab":
    """Load *labnames* from the cell's lab file *path*: ``load_lab(search_paths=[path])``.

    *kwargs* go to :func:`otto.load_lab`, whose default view renders a
    ``.jsonnet`` cell (:func:`otto.labs.loader.search_paths_view`). The frames
    the cell's hosts name must be registered first (``custom_hosts``).
    """
    import otto

    return otto.load_lab(labnames, search_paths=[path], **kwargs)


def cell_hosts(path: Path) -> list[dict[str, Any]]:
    """Return every host entry of the cell's lab file *path* as a flat dict, in document order.

    THE scripts' one walk of the document's ``elements[].hosts[]``
    (:func:`read_cell_document`, so passwords arrive wrapped). Since lab.json
    v2 a host entry lives under its element and no longer carries
    ``element``/``element_id`` itself: they are stamped back on here, which is
    exactly what otto's own loader does before host validation.
    """
    hosts: list[dict[str, Any]] = []
    for element in read_cell_document(path)["elements"]:
        for host in element["hosts"]:
            flat = {**host, "element": element["name"]}
            if element.get("id") is not None:
                flat["element_id"] = element["id"]
            hosts.append(flat)
    return hosts


def management_addresses(path: Path) -> list[str]:
    """Return every host entry's ``ip`` in the cell's lab file *path*, in document order."""
    return [host["ip"] for host in cell_hosts(path) if "ip" in host]


def main(argv: "list[str] | None" = None) -> int:
    """Run ``reach``: print the cell's management addresses, one per line; 0 read, 1 not."""
    parser = argparse.ArgumentParser(description="read a bed cell's lab file")
    verbs = parser.add_subparsers(dest="verb", required=True)
    reach = verbs.add_parser("reach", help="print every host's management address, one per line")
    reach.add_argument("lab", type=Path, help="the cell's lab file (lab.jsonnet)")
    args = parser.parse_args(argv)
    from otto.labs.errors import LabRepositoryError

    try:
        addresses = management_addresses(args.lab)
    except LabRepositoryError as exc:
        print(f"_lab_document: {exc}", file=sys.stderr)
        return 1
    for address in addresses:
        print(address)
    return 0


if __name__ == "__main__":
    sys.exit(main())
