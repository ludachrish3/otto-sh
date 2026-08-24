"""Imports otto_fixture_beetroot at module level -- the eager failure shape.

When the fixture package is absent this raises at bootstrap, inside the per-repo
BootstrapError frame. Paired with the lazy shape in the instruction beside it,
so the fixture carries BOTH ways a missing Python dependency surfaces.
"""

import otto_fixture_beetroot


def beet_twice() -> str:
    return otto_fixture_beetroot.beet() * 2
