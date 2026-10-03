"""Refusals raised by the ``otto init`` library, importable without the library itself.

``otto init`` catches :class:`InitInputError` at its one translation site
(:func:`otto.cli.invoke.usage_error_from`). This module imports only
:mod:`otto.errors`, so catching it never pulls the doctor or the scaffolder
onto a CLI path (import budget).
"""

from ..errors import FieldError


class InitInputError(FieldError):
    """An ``otto init`` input refusal; ``field`` is the parameter at fault.

    One of ``"root"``, ``"kmodcov_dir"`` or ``"areas"``. The message names no
    CLI flag: a CLI spells the field in its own flags at
    :func:`otto.cli.invoke.usage_error_from`.
    """
