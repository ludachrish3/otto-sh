"""Where the vendored areas land by default, relative to the project root.

A leaf with no imports: ``otto init``'s flags read their defaults from here at
import time, which the CLI's import budget allows only for a module this cheap,
and :class:`~otto.init.config.InitConfig`, detection and the doctor read the
same values.
"""

DEFAULT_KMODCOV_DIR = "third_party/otto_kmodcov"
"""Where the kmodcov area vendors the otto_kmodcov library, relative to the project root."""

DEFAULT_EMBEDDED_GCOV_DIR = "third_party/embedded-gcov-patch"
"""Where the embedded-gcov area writes otto's runtime patch, relative to the project root."""
