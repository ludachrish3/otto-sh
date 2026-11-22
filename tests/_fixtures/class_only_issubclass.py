"""``issubclass`` held to the rule pydantic before 2.11 applies, whatever pydantic is installed.

A pydantic model's metaclass before 2.11 inherits ``ABCMeta``'s
``__subclasscheck__``, which raises ``TypeError`` for a first argument that
is not a class. On Python 3.10 a generic alias (``list[X]``,
``dict[str, int]``) answers ``isinstance(..., type)`` ``True`` without being
one, so a check that asks ``isinstance`` before ``get_origin`` reaches that
raise there. :func:`class_only_issubclass` raises the same way for every
parent, so a test that installs it (:func:`install`) fails on any 3.10 when
the origin is not read first. On 3.11 and later a generic alias is no
instance of ``type``, and the check never gets that far.
"""

import builtins
from typing import Any

import pytest

_REAL_ISSUBCLASS = builtins.issubclass


def class_only_issubclass(cls: Any, parent: Any) -> bool:
    """``issubclass`` as ``ABCMeta`` judges it: a first argument that is not a class raises."""
    if not _REAL_ISSUBCLASS(type(cls), type):
        raise TypeError("issubclass() arg 1 must be a class")
    return _REAL_ISSUBCLASS(cls, parent)


def install(monkeypatch: pytest.MonkeyPatch, *, strict: bool) -> None:
    """Install :func:`class_only_issubclass` as ``issubclass`` for the test when *strict*."""
    if strict:
        monkeypatch.setattr(builtins, "issubclass", class_only_issubclass)
