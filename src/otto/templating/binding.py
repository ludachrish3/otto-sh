"""The one module that imports a Jsonnet binding.

otto installs one binding per platform, chosen by marker in ``pyproject.toml``:
the C++ ``jsonnet`` (module ``_jsonnet``) where it ships a wheel, and the Go
``gojsonnet`` (module ``_gojsonnet``) on Linux aarch64.
:func:`load_binding` tries them in one fixed order, C++ first, and memoizes
the first that imports. Nothing selects a binding: no setting and no
environment variable. The order only matters in an environment someone built
by hand with both installed.

The import happens inside :func:`load_binding`, never at module scope, so a
process that renders nothing loads neither binding
(``tests/unit/templating/test_binding.py`` scans ``src/otto`` for any other
site).
"""

import functools
import platform
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Protocol

from .errors import TemplatingUnavailableError


class JsonnetBinding(Protocol):
    """The contract both bindings share, the shape their ``evaluate_snippet`` takes.

    *name* is the binding's distribution name, ``jsonnet`` or ``gojsonnet``.
    ``import_callback(dir, rel)`` returns the resolved path and the file's bytes.
    A failed evaluation raises ``RuntimeError``, whose first frame is
    ``<file>:<line>:<col>``; the wording otherwise differs between the bindings.
    """

    @property
    def name(self) -> str:
        """The binding's distribution name, ``jsonnet`` or ``gojsonnet``."""
        ...

    def evaluate_snippet(
        self,
        filename: str,
        src: str,
        *,
        tla_codes: Mapping[str, str],
        import_callback: Callable[[str, str], tuple[str, bytes]],
        max_stack: int,
        max_trace: int,
    ) -> str:
        """Evaluate *src*, reported as *filename*, and return the JSON it manifests."""
        ...


@dataclass(frozen=True)
class _ModuleBinding:
    """A binding module, named by the distribution that installs it."""

    name: str
    module: ModuleType

    def evaluate_snippet(
        self,
        filename: str,
        src: str,
        *,
        tla_codes: Mapping[str, str],
        import_callback: Callable[[str, str], tuple[str, bytes]],
        max_stack: int,
        max_trace: int,
    ) -> str:
        """Forward to the module's ``evaluate_snippet``, which takes a plain ``dict``."""
        return self.module.evaluate_snippet(
            filename,
            src,
            tla_codes=dict(tla_codes),
            import_callback=import_callback,
            max_stack=max_stack,
            max_trace=max_trace,
        )


def _unavailable_message() -> str:
    """Spell the one "no binding" message, naming the platform and the Python."""
    return (
        f"Jsonnet templating is not available on {platform.system()} {platform.machine()} "
        f"(Python {sys.version_info.major}.{sys.version_info.minor}): otto installs no "
        "Jsonnet binding there. Install one yourself (pip install jsonnet builds it from "
        "source) or render on a supported platform."
    )


@functools.cache
def load_binding() -> JsonnetBinding:
    """Return the installed Jsonnet binding: ``_jsonnet`` (C++) first, then ``_gojsonnet``.

    Memoized, so the import is tried once per process. Raises
    :class:`~otto.templating.errors.TemplatingUnavailableError` when neither
    imports; that failure is not memoized.

    Both modules are compiled extensions with no type stubs, which no static
    checker resolves on any platform, hence the ``import-not-found`` ignores.
    """
    try:
        import _jsonnet  # ty: ignore[import-not-found]

        return _ModuleBinding("jsonnet", _jsonnet)
    except ImportError:
        pass
    try:
        import _gojsonnet  # ty: ignore[import-not-found]

        return _ModuleBinding("gojsonnet", _gojsonnet)
    except ImportError:
        pass
    raise TemplatingUnavailableError(_unavailable_message())
