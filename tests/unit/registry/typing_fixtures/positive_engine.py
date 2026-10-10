"""Positive typing fixture: the engine's typed surface checks clean under ty.

``make typecheck-python`` runs ``ty check`` on this directory explicitly
(``pyproject.toml`` excludes tests from ty). Everything lives inside functions,
so importing the module (pytest's doctest collection does) builds no table.
"""

from collections.abc import Callable
from dataclasses import dataclass

from otto.registry import (
    BackendRegistry,
    ClassEntry,
    Configured,
    Ref,
    Registry,
    Subscription,
    configured_backend,
    resolved,
)


class Frame:
    """A class a class-valued registry holds."""


@dataclass(frozen=True)
class StoreConfig:
    """A configuration model (duck-typed: the engine never imports pydantic)."""

    path: str


@dataclass(frozen=True)
class StoreEnv:
    """The environment a backend seam supplies."""

    root: str


class Store:
    """What the backend builds."""


def _store(c: Configured[StoreConfig, StoreEnv]) -> Store:
    return Store()


def class_valued_registry() -> Frame:
    frames: Registry[ClassEntry[Frame]] = Registry(
        "frame",
        entry=ClassEntry,
        register_hint="register_frame()",
        check_resolved=lambda name, entry: None,
    )
    frames.register("a", ClassEntry(Frame))
    frames.register("b", ClassEntry(Ref("m:Frame")))
    frames.register_many([("c", ClassEntry(Frame))], overwrite=True)
    return resolved(frames.get("a").cls)()


def subscription() -> None:
    hooks: Subscription[Callable[[int], int]] = Subscription("hook", register_hint="x")
    token = hooks.subscribe(abs)
    token.cancel()
    for occurrence in hooks.items():
        occurrence.value(1)


def backend_registry() -> Store:
    stores: BackendRegistry[StoreEnv, Store, None] = BackendRegistry(
        "store",
        register_hint="register_store()",
        error=ValueError,
        describe_parse_error=str,
        result=lambda name, obj: None,
    )
    stores.register("json", configured_backend(config=StoreConfig, factory=_store, metadata=None))
    return stores.build(stores.prepare("json", {"path": "p"}, StoreEnv("r"), source="settings"))
