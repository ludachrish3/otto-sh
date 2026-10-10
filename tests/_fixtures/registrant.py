"""Register from a synthetic module: the engine credits the calling frame's module."""

from collections.abc import Callable
from typing import Any


def from_module(module: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call *fn* from a frame whose module is *module* (attribution is by frame)."""
    code = compile("result = fn(*args, **kwargs)", f"<{module}>", "exec")
    scope = {"__name__": module, "fn": fn, "args": args, "kwargs": kwargs}
    exec(code, scope)  # noqa: S102 — a synthetic registrant module
    return scope["result"]
