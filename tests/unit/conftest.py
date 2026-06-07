"""
Unit-tree conftest.

The parametrized host fixtures (``host1`` / ``host2`` / ``host3`` /
``hop_host`` / ``transfer_host``) and the ``host_data`` / ``make_host``
helpers used to live here. They moved to ``tests/conftest.py`` so the
``tests/integration/host/`` tree can use them too without import gymnastics.
The unit tests inherit them transparently through the conftest hierarchy —
no changes needed at the call sites.

This file is kept as a hook point for unit-tree-only fixtures or behavior
should any need to be added in the future.
"""
