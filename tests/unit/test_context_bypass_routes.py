"""A route around the run context's facade reaches a context that does not compose.

The composition root, ``otto.bootstrap``, composes the repos. ``otto.context``'s resolver
loads it for ``OttoContext`` and ``open_context``, and the run context below it,
``otto.run_context``, never imports it (spec 2026-10-11 placement-and-phases §3.9,
§3.12). A route that reaches the class without that resolver, in a process that never
loaded the root, gets a class whose first composing read refuses with the catalog's
refusal, which names both ways to compose, and an ``open_context`` whose entry
refuses the same way before it installs or yields a context. Each case runs in a
fresh interpreter over
a scratch repo (``tests/_fixtures/fresh_composition.py``), composes nothing, and then
arms the counting hook, so its 0 is a count the hook would have made.
"""

import pickle
from pathlib import Path

import pytest

from tests._fixtures.fresh_composition import armed, compositions, run_fresh

_REFUSAL = (
    "Nothing composed otto's repos, and no composition root is loaded to compose them: "
    "call otto.bootstrap.bootstrap(), or open a run with otto.open_context(...)."
)

_READ = """
import json, pickle, sys, typing
{reach}
from otto.lab import Lab
root = "otto.bootstrap" in sys.modules
try:
    read = [r.name for r in cls(lab=Lab(name="pin")).repos]
except RuntimeError as exc:
    read = str(exc)
print(json.dumps([cls.__module__, root, read]))
from otto import bootstrap as _arm
_arm.bootstrap()
"""

_ROUTES = {
    "the implementation module": "from otto.run_context import OttoContext as cls",
    "a type hint of a facade function": (
        "from otto.context import get_context\ncls = typing.get_type_hints(get_context)['return']"
    ),
    "an unpickled class": "cls = pickle.loads(bytes.fromhex({cls_hex!r}))",
    "an unpickled context's class": "cls = type(pickle.loads(bytes.fromhex({ctx_hex!r})))",
}


@pytest.mark.parametrize("route", list(_ROUTES))
def test_a_route_around_the_facade_reaches_a_context_that_refuses(
    route: str, tmp_path: Path
) -> None:
    from otto.lab import Lab
    from otto.run_context import OttoContext

    reach = _ROUTES[route].format(
        cls_hex=pickle.dumps(OttoContext).hex(),
        ctx_hex=pickle.dumps(OttoContext(lab=Lab(name="pin"))).hex(),
    )
    assert run_fresh(tmp_path, _READ.format(reach=reach)) == ["otto.run_context", False, _REFUSAL]
    assert armed(tmp_path)
    assert compositions(tmp_path) == 1, "only the control's bootstrap() composes"


_ENTER = """
import asyncio, json, pickle, sys
{reach}
from otto.lab import Lab
from otto.run_context import try_get_context
root = "otto.bootstrap" in sys.modules


async def enter():
    async with fn(lab=Lab(name="pin")):
        return "yielded"


try:
    read = asyncio.run(enter())
except RuntimeError as exc:
    read = str(exc)
print(json.dumps([fn.__module__, root, read, try_get_context() is None]))
from otto import bootstrap as _arm
_arm.bootstrap()
"""

_ENTRIES = {
    "the implementation module": "from otto.run_context import open_context as fn",
    "an unpickled function": "fn = pickle.loads(bytes.fromhex({fn_hex!r}))",
}


@pytest.mark.parametrize("route", list(_ENTRIES))
def test_a_route_around_the_facade_enters_an_open_context_that_refuses(
    route: str, tmp_path: Path
) -> None:
    from otto.run_context import open_context

    reach = _ENTRIES[route].format(fn_hex=pickle.dumps(open_context).hex())
    assert run_fresh(tmp_path, _ENTER.format(reach=reach)) == [
        "otto.run_context",
        False,
        _REFUSAL,
        True,
    ]
    assert armed(tmp_path)
    assert compositions(tmp_path) == 1, "only the control's bootstrap() composes"
