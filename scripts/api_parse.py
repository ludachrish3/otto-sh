r"""Parse Python source the public-surface scripts read, without its warnings becoming errors.

A file or docs block the scripts read is someone else's code: an invalid
escape (``"\d"`` in a normal string) is a ``SyntaxWarning`` from Python 3.12
and a ``DeprecationWarning`` before it. Where warnings are errors (pytest runs
with ``filterwarnings = error``), the compiler turns that warning into a
``SyntaxError`` -- so a module that parses fine would read as unparseable,
and a static scan would lose its docstrings or its bindings. Every
``ast.parse`` in the v2 tooling goes through :func:`parse_quietly`.

A leaf module: it imports nothing from ``scripts``.
"""

import ast
import warnings


def parse_quietly(source: str, filename: str = "<unknown>") -> ast.Module:
    """``ast.parse`` *source* with ``SyntaxWarning``/``DeprecationWarning`` ignored.

    A real syntax error still raises ``SyntaxError`` (or ``ValueError`` for a
    NUL byte before 3.12), exactly as ``ast.parse`` does.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        return ast.parse(source, filename=filename)
