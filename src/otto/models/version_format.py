"""The one pattern for a product version string: ``MAJOR.MINOR.PATCH`` plus an optional tag.

``otto.models.settings`` validates ``version`` with it at load and
``otto.config.version.Version`` parses with it; both call
``version_re.fullmatch`` so a trailing newline is refused. The module imports
only ``re``.
"""

import re

version_re = re.compile(
    r"(?P<major>\d+)\."
    r"(?P<minor>\d+)\."
    r"(?P<patch>\d+)"
    r"(?P<extra>[-+.][0-9A-Za-z.+-]+)?"
)
"""Optional extra tag begins with ``-``, ``+`` or ``.``; apply with ``fullmatch``."""
