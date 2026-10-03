"""A user-style module with string annotations, for the class-factory tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from otto.host.declared_product import DeclaredProduct


@dataclass
class Stringy(DeclaredProduct):
    LIMIT: ClassVar[int] = 3
    slot: int = 0
