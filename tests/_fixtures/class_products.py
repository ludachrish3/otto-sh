"""DeclaredProduct subclasses a ``class =`` entry can name, for the class-factory tests."""

from dataclasses import dataclass, field
from pathlib import Path

from otto.host.declared_product import DeclaredProduct
from otto.result import Result
from otto.utils import Status


class OverridesInstall(DeclaredProduct):
    async def install(self, host):
        return await host.run(f"fwload {self.stage_dir}/{self.artifact.name}")


@dataclass
class WithFields(DeclaredProduct):
    slot: int = 0
    label: str = "none"
    strict: bool = False
    extra_dir: Path = Path("/tmp")
    tags: list[str] = field(default_factory=list)
    whatever: object = None


@dataclass
class WithComputed(DeclaredProduct):
    slot: int = 0
    computed: int = field(init=False, default=0)


@dataclass(kw_only=True)
class RequiredField(DeclaredProduct):
    channel: str  # no default: the entry must set it


class NotAProduct:
    pass


def not_a_class(entry, host):
    return Result(Status.Success)
