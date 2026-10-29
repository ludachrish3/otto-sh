"""tests/unit/docker — every test stages onto a parent whose private root otto can trust.

Docker staging checks the parent's private staging root before every write and
read-back. The tests here mock the parent's ``exec`` for what they are about
(a build, a deploy, a teardown, a preview), so the root check is answered for
them by :func:`~tests._fixtures.private_root.trusted_private_roots`. A test
about the check itself carries ``@pytest.mark.real_private_roots`` and gets the
real one, driven by a parent that answers the real probe command.
"""

import pytest

from tests._fixtures.private_root import trusted_private_roots  # noqa: F401 — fixture


@pytest.fixture(autouse=True)
def _private_roots(request):
    if request.node.get_closest_marker("real_private_roots") is not None:
        return
    request.getfixturevalue("trusted_private_roots")
