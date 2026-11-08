"""Every public otto error survives a pickle round-trip.

Spec 2026-11-07-keyword-only-public-interfaces-design.md §6.5:
``BaseException.__reduce__`` rebuilds as ``cls(*self.args)``, so an error whose
``__init__`` takes keyword-only parameters, or whose ``args`` is not shaped like
its constructor, is unpicklable unless it defines ``__reduce__``. Errors cross
process boundaries (``otto.coverage`` workers, pytest-xdist), so every class in
the golden whose ``mro`` contains ``otto.errors:OttoError`` is built from its
signature with a sample per annotation and round-tripped.

A class the sample table cannot build FAILS, naming the annotation: the table
grows, a class is never skipped.
"""

import ast
import asyncio
import dataclasses
import enum
import functools
import importlib
import inspect
import pickle
import sys
import types
import typing
from pathlib import Path
from typing import Any, Union, get_args, get_origin

import pytest

from otto.errors import OttoError
from tests._fixtures.paths import CHECKOUT_ROOT

_GOLDEN = CHECKOUT_ROOT / "tests" / "unit" / "api_snapshot" / "public_api.txt"


def _error_keys() -> list[str]:
    keys = []
    for line in _GOLDEN.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if fields[0] == "mro" and "{otto.errors:OttoError}" in fields[2].split():
            keys.append(fields[1])
    keys.append("otto.errors:OttoError")
    return sorted(set(keys))


def _resolve(key: str) -> type:
    module, _, qualname = key.partition(":")
    obj: Any = importlib.import_module(module)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


class UnbuildableError(Exception):
    """The sample table has no entry for an annotation."""


_SCALARS: dict[Any, Any] = {
    str: "x",
    int: 1,
    float: 1.0,
    bool: False,
    Path: Path("p"),
    bytes: b"x",
}


@functools.cache
def _type_checking_names(module_name: str) -> dict[str, Any]:
    """The names *module_name* imports only under ``if TYPE_CHECKING:``.

    ``typing.get_type_hints`` cannot see them at run time; they are imported
    here so an annotation naming one resolves.
    """
    module = sys.modules[module_name]
    tree = ast.parse(inspect.getsource(module))
    namespace: dict[str, Any] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test)):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.ImportFrom):
                continue
            package = module.__package__ if stmt.level else None
            target = importlib.import_module("." * stmt.level + (stmt.module or ""), package)
            for alias in stmt.names:
                namespace[alias.asname or alias.name] = getattr(target, alias.name)
    return namespace


def _build_from_signature(cls: type) -> Any:
    """Construct *cls* with a sample for every required parameter."""
    init = cls.__init__
    hints = typing.get_type_hints(init, localns=_type_checking_names(init.__module__))
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for name, param in inspect.signature(cls).parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if param.default is not param.empty:
            continue
        if name not in hints:
            raise UnbuildableError(f"parameter {name} has no annotation")
        value = _sample(hints[name])
        if param.kind is param.KEYWORD_ONLY:
            kwargs[name] = value
        else:
            args.append(value)
    return cls(*args, **kwargs)


def _closed_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    loop.close()
    return loop


def _sample_generic(annotation: Any) -> Any:
    """A sample for a parameterized annotation, or ``_NO_SAMPLE``."""
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, getattr(types, "UnionType", Union)):
        return None if type(None) in args else _sample(args[0])
    if annotation in (list, tuple) or origin in (list, tuple, typing.Sequence):
        return []
    if annotation is dict or origin in (dict, typing.Mapping):
        return {}
    if origin is typing.Literal:
        return args[0]
    return _NO_SAMPLE


def _sample_class(annotation: type) -> Any:
    """A sample for a class annotation, or ``_NO_SAMPLE``."""
    if issubclass(annotation, enum.Enum):
        return next(iter(annotation))
    if issubclass(annotation, OttoError):
        return _build(annotation)
    if issubclass(annotation, BaseException):
        return annotation("x")
    if dataclasses.is_dataclass(annotation):
        return _build_from_signature(annotation)
    return _NO_SAMPLE


_NO_SAMPLE = object()


def _sample(annotation: Any) -> Any:
    if annotation is Any:
        return "x"
    if annotation is asyncio.AbstractEventLoop:
        return _closed_loop()
    if annotation in _SCALARS:
        return _SCALARS[annotation]
    value = _sample_generic(annotation)
    if value is _NO_SAMPLE and isinstance(annotation, type):
        value = _sample_class(annotation)
    if value is _NO_SAMPLE:
        raise UnbuildableError(f"no sample for annotation {annotation!r}")
    return value


def _build(cls: type) -> BaseException:
    if not inspect.isfunction(cls.__init__):
        return cls("x")  # a builtin's constructor: one message
    return _build_from_signature(cls)


@pytest.mark.parametrize("key", _error_keys())
def test_public_error_round_trips(key: str) -> None:
    cls = _resolve(key)
    try:
        exc = _build(cls)
    except UnbuildableError as err:
        pytest.fail(f"{key}: cannot build from its signature: {err}")
    clone = pickle.loads(pickle.dumps(exc))  # noqa: S301 — round trip of an error this test just built
    assert type(clone) is type(exc), key
    assert str(clone) == str(exc), key
    _assert_same_attributes(clone, exc, key)


def _assert_same_attributes(clone: BaseException, exc: BaseException, key: str) -> None:
    """Same attribute names; equal values wherever the value's type defines ``==``.

    A value whose type keeps ``object.__eq__`` (an event loop, say) compares by
    identity, which no unpickled copy can satisfy, so for it the type is compared.
    """
    assert vars(clone).keys() == vars(exc).keys(), key
    for name, value in vars(exc).items():
        copied = vars(clone)[name]
        if type(value).__eq__ is object.__eq__:
            assert type(copied) is type(value), f"{key}.{name}"
        else:
            assert copied == value, f"{key}.{name}"


def test_a_coverage_error_keeps_its_report_across_a_pickle() -> None:
    """The structure beside the message survives, not only the message."""
    from otto.coverage import CoverageNotInstrumentedError
    from otto.coverage.instrumentation import InstrumentationReport, InstrumentationRow

    row = _build_from_signature(InstrumentationRow)
    exc = CoverageNotInstrumentedError("x", report=InstrumentationReport(rows=[row]))
    clone = pickle.loads(pickle.dumps(exc))  # noqa: S301 — round trip of an error this test just built
    assert clone.report == exc.report
    assert clone.report.rows == [row]


_OS_NATIVES = ("errno", "strerror", "filename", "filename2")


def _os_error_keys() -> list[str]:
    return [key for key in _error_keys() if issubclass(_resolve(key), OSError)]


@pytest.mark.parametrize("key", _os_error_keys())
def test_an_os_error_keeps_its_native_attributes_across_a_pickle(key: str) -> None:
    """``errno``/``strerror``/``filename``/``filename2`` live in C slots, not ``__dict__``.

    The stdlib's ``OSError.__reduce__`` carries them; otto's reducer must too.
    The natives are set after construction so a class with its own ``__init__``
    is covered as well as one ``OSError.__new__`` parses.
    """
    exc = _build(_resolve(key))
    assert isinstance(exc, OSError)
    exc.errno, exc.strerror, exc.filename, exc.filename2 = 2, "missing", "a.py", "b.py"
    clone = pickle.loads(pickle.dumps(exc))  # noqa: S301 — round trip of an error this test just built
    for name in _OS_NATIVES:
        assert getattr(clone, name) == getattr(exc, name), f"{key}.{name}"
    assert str(clone) == str(exc), key


def test_a_session_setup_error_keeps_its_filename_across_a_pickle() -> None:
    """The case the branch review reproduced: three positionals parsed by ``OSError``."""
    from otto.host.errors import SessionSetupError

    exc = SessionSetupError(2, "missing", "setup.py")
    clone = pickle.loads(pickle.dumps(exc))  # noqa: S301 — round trip of an error this test just built
    assert (clone.errno, clone.strerror, clone.filename) == (2, "missing", "setup.py")
    assert str(clone) == str(exc) == "[Errno 2] missing: 'setup.py'"
