creds
=====

The creds package supplies credentials by inventory key from a store selected
by ``[creds]`` in ``~/.otto/settings.toml`` (or a project's override). The
inventory wraps the selected store in
:class:`otto.inventory.creds.CredsOverlay`, which merges its entries under the
record's by login; the lab file's entries layer over both.

For configuration and the merge rules see
:doc:`../configuration/inventory`; for writing a store of your own,
:doc:`../cookbook/extending/creds-backends`.

.. automodule:: otto.creds
