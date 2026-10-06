"""Produce the API dump records for one source tree's declared namespaces.

Run as a FILE, in a fresh interpreter: ``python scripts/api_dump_child.py --src
<dir> [--assume-dir] [--format NAME READS WRITES]... <namespace>...``.
``scripts/api_regen.py`` runs it with a commit archive's ``src`` and
dependency-only environment; ``make api-snapshot`` runs it on the working tree.
It puts ``--src`` first on ``sys.path``, imports every namespace, and prints ONE
JSON object as its last line of output: ``records`` (record lines, producer
order), ``refusals``, ``provenance`` and ``private`` (the underscore members of
each declared class, for the docs validator's ``taught-private-member`` check).

Spec: ``docs/superpowers/specs/2026-10-05-api-dump-design.md`` §3 (records),
§5.3 (provenance), §7.2 (hidden obligations), §13.2 (format records). Imports
only the standard library, ``typing_extensions`` and ``api_records`` (a sibling
file); pydantic is read from ``sys.modules`` and never imported here.
"""

import argparse
import ast
import dataclasses
import enum
import functools
import importlib
import inspect
import json
import os
import sys
import tokenize
import types
import typing

PACKAGE = "otto"
_FIELD = object()  # a member that is a declared field, not a class-dict entry
_PARAM_KIND = {
    inspect.Parameter.POSITIONAL_ONLY: "PO",
    inspect.Parameter.POSITIONAL_OR_KEYWORD: "PK",
    inspect.Parameter.VAR_POSITIONAL: "VP",
    inspect.Parameter.KEYWORD_ONLY: "KO",
    inspect.Parameter.VAR_KEYWORD: "VK",
}


def _opaque(encoded: str) -> bool:
    """Return True when an encoded value is, or holds anywhere inside it, the opaque ``O``."""
    if encoded == "O":
        return True
    if encoded[:2] in ("T:", "Z:"):
        return any(_opaque(item) for item in json.loads(encoded[2:]))
    if encoded.startswith("E:"):
        return _opaque(encoded.partition("=")[2])
    return False


class _RefuseError(Exception):
    """One binding or member the producer will not record (dump spec §3)."""


def _why(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _literal_list(module: types.ModuleType, const: str) -> "list | None":
    """Return the literal list *module*'s source assigns to *const* at top level, else None.

    The LAST top-level assignment wins, as at runtime. Any non-literal element (a
    name, a call, a comprehension) makes the whole value non-literal.
    """
    path = getattr(module, "__file__", None)
    if not path or not path.endswith(".py"):
        return None
    with tokenize.open(path) as fh:  # honours a PEP 263 coding cookie
        tree = ast.parse(fh.read(), path)
    found: "list | None" = None
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets = [stmt.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == const for t in targets):
            value = stmt.value
            literal = isinstance(value, ast.List) and all(
                isinstance(e, ast.Constant) for e in value.elts
            )
            found = [e.value for e in value.elts] if literal else None
    return found


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _is_private(name: str) -> bool:
    return name.startswith("_") and not _is_dunder(name)


# Classification reads an object's REAL type, ``type(obj)``, never ``isinstance``,
# which also trusts whatever ``__class__`` the object claims. (A class cannot
# claim another metaclass: ``__class__`` on a class is read from the metaclass.)


def _is_class(obj: object) -> bool:
    return issubclass(type(obj), type) and typing.get_origin(obj) is None


# The real types of a typing alias. typing.get_origin alone trusts __class__.
_ALIAS_TYPES = tuple(
    t
    for t in (
        types.GenericAlias,
        getattr(types, "UnionType", None),
        getattr(typing, "_BaseGenericAlias", None),
        typing.ParamSpecArgs,
        typing.ParamSpecKwargs,
    )
    if t is not None
)


def _is_alias(obj: object) -> bool:
    if typing.get_origin(obj) is None:
        return False
    return obj is typing.Generic or issubclass(type(obj), _ALIAS_TYPES)


class _DeleteProbe:
    def __get__(self, obj: object, owner: object = None) -> None: ...
    def __delete__(self, obj: object) -> None: ...


# What this interpreter's inspect.isroutine counts, re-read off the real type: a
# method-wrapper counts where inspect.ismethodwrapper exists, and a descriptor
# with __delete__ stops counting where inspect.ismethoddescriptor says so.
_ROUTINE_TYPES = (types.FunctionType, types.BuiltinFunctionType, types.MethodType) + (
    (types.MethodWrapperType,) if hasattr(inspect, "ismethodwrapper") else ()
)
_DELETE_IS_NOT_A_METHOD_DESCRIPTOR = not inspect.ismethoddescriptor(_DeleteProbe())


def _is_routine(obj: object) -> bool:
    """Return ``inspect.isroutine(obj)``, judged by *obj*'s real type."""
    tp = type(obj)
    if issubclass(tp, _ROUTINE_TYPES):
        return True
    if issubclass(tp, type):
        return False
    return (
        hasattr(tp, "__get__")
        and not hasattr(tp, "__set__")
        and not (_DELETE_IS_NOT_A_METHOD_DESCRIPTOR and hasattr(tp, "__delete__"))
    )


# Attributes through which inspect.signature reads a signature from somewhere other
# than the function's own code: an explicit signature, a partialmethod's, or a
# coroutine marker that inspect.iscoroutinefunction honours on newer interpreters.
_OVERRIDES = ("__signature__", "_partialmethod", "__partialmethod__", "_is_coroutine_marker")


class CallKindError(_RefuseError):
    """A callable whose call kind the producer is not certain of (dump spec §3.2)."""

    def __init__(self, shape: str) -> None:
        super().__init__(f"call kind cannot be determined for {shape}")


def _override(obj: object, *extra: str) -> "str | None":
    """Return the first of *extra* and the override attributes *obj* carries, else None."""
    return next((name for name in (*extra, *_OVERRIDES) if hasattr(obj, name)), None)


def _end_of_chain(fn: object, what: str) -> types.FunctionType:
    """Follow *fn*'s ``__wrapped__`` links to the last one; every link is a plain function."""
    seen: set[int] = set()
    while True:
        if type(fn) is not types.FunctionType:  # FunctionType cannot be subclassed
            raise CallKindError(f"{what} reached through a {type(fn).__name__}")
        override = _override(fn)
        if override is not None:
            raise CallKindError(f"{what} carrying {override}")
        if id(fn) in seen:
            raise CallKindError(f"{what} with a cyclic __wrapped__ chain")
        seen.add(id(fn))
        if not hasattr(fn, "__wrapped__"):
            return fn
        fn = fn.__wrapped__


def check_metaclass_call(klass: type) -> None:
    """Refuse *klass* when its metaclass defines a ``__call__`` of its own.

    Such a ``__call__`` decides what ``klass(...)`` accepts, and the producer
    does not model it. ``type.__call__`` (``abc.ABCMeta``, pydantic's
    metaclass) and the enum metaclass's (enums have their own records) pass.
    """
    owner = next(m for m in type(klass).__mro__ if "__call__" in vars(m))
    if owner not in (type, enum.EnumMeta):
        raise _RefuseError(
            f"constructor routed through metaclass {owner.__qualname__}.__call__ cannot be recorded"
        )


def _drop_receiver(sig: inspect.Signature) -> inspect.Signature:
    """Apply the receiver rule: drop a leading ``PO`` or ``PK`` parameter only."""
    params = list(sig.parameters.values())
    if params and params[0].kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ):
        return sig.replace(parameters=params[1:])
    return sig


def _own_signature(fn: types.FunctionType) -> inspect.Signature:
    return inspect.signature(fn, follow_wrapped=False)


def call_target(obj: object) -> "tuple[types.FunctionType | type, inspect.Signature]":
    """Return the function or class *obj*'s call kind is read from, and its derived signature.

    Only a closed set of shapes is resolved; anything else raises
    :class:`CallKindError`, never a guess:

    - a plain function, followed along ``__wrapped__`` links that are all plain
      functions, to the last one;
    - a bound method over such a function, receiver dropped;
    - an exact ``functools.partial`` with no ``__wrapped__`` or ``__signature__``
      of its own, over a function, a bound method, a class or another such
      partial, its bound arguments applied to the inner signature;
    - a callable instance with no ``__wrapped__``, ``__signature__`` or
      ``__code__``, whose type's ``__call__`` (a static lookup along the MRO, no
      descriptor call) is such a function, receiver dropped.

    The derived signature is built from the target alone, so a caller that
    compares it with ``inspect.signature(obj)`` proves both read one object.
    A class is its own target (its kind is always ``sync``), unless its
    metaclass defines a ``__call__`` of its own.
    """
    if type(obj) is types.FunctionType:
        fn = _end_of_chain(obj, "a function")
        return fn, _own_signature(fn)
    if type(obj) is types.MethodType:  # MethodType cannot be subclassed
        fn = _end_of_chain(obj.__func__, "a bound method")
        return fn, _drop_receiver(_own_signature(fn))
    if issubclass(type(obj), functools.partial):
        if type(obj) is not functools.partial:
            raise CallKindError(f"a partial subclass {type(obj).__name__}")
        override = _override(obj, "__wrapped__")
        if override is not None:
            raise CallKindError(f"a partial carrying {override}")
        func = obj.func
        supported = (types.FunctionType, types.MethodType, functools.partial)
        if not (_is_class(func) or issubclass(type(func), supported)):
            raise CallKindError(f"a partial over a {type(func).__name__}")
        fn, inner = call_target(func)
        return fn, _apply_partial(inner, obj)
    if _is_class(obj):
        check_metaclass_call(obj)
        return obj, inspect.signature(obj)
    if _is_routine(obj):
        raise CallKindError(f"a {type(obj).__name__}")
    override = _override(obj, "__wrapped__", "__code__")  # __code__: inspect reads it as a function
    if override is not None:
        raise CallKindError(f"a callable {type(obj).__name__} instance carrying {override}")
    owner = next((c for c in type(obj).__mro__ if "__call__" in vars(c)), None)
    call = vars(owner)["__call__"] if owner is not None else None
    if type(call) is not types.FunctionType:
        raise CallKindError(
            f"a callable {type(obj).__name__} instance whose __call__ is a {type(call).__name__}"
        )
    fn = _end_of_chain(call, f"a callable {type(obj).__name__} instance's __call__")
    return fn, _drop_receiver(_own_signature(fn))


def _apply_partial(inner: inspect.Signature, part: functools.partial) -> inspect.Signature:
    """Return *inner* with *part*'s bound arguments applied, as ``inspect`` applies them."""

    def stub(*args: object, **kwargs: object) -> None:
        del args, kwargs

    stub.__signature__ = inner
    return inspect.signature(functools.partial(stub, *part.args, **part.keywords))


def code_kind(target: "types.FunctionType | type") -> str:
    """Return a call target's kind: a class is ``sync``; a function's comes from its code flags."""
    if _is_class(target):
        return "sync"
    flags = target.__code__.co_flags
    if flags & inspect.CO_ASYNC_GENERATOR:
        return "asyncgen"
    if flags & inspect.CO_COROUTINE:
        return "coroutine"
    return "sync"


class Producer:
    """Collect every public binding first, then emit each one's records."""

    def __init__(self, api: types.ModuleType, src: str, assume_dir: bool) -> None:
        self.api = api
        self.src = os.path.realpath(src)
        self.assume_dir = assume_dir
        self.records: list = []
        self.refusals: list[str] = []
        self.private: dict[str, list[str]] = {}
        self.bindings: dict[str, object] = {}
        self.paths: dict[str, list[tuple[int, str]]] = {}  # key -> enclosing (id, key) chain
        self.keys_by_id: dict[int, list[str]] = {}

    # -- collection -------------------------------------------------------

    def run(self, namespaces: "list[str]", formats: "list[list[str]] | None" = None) -> dict:
        """Collect *namespaces*, emit every binding and format, and return the JSON report."""
        for ns in namespaces:
            self._collect_namespace(ns)
        for key in sorted(self.bindings):
            self._emit(key, self.bindings[key])
        for spec in formats or []:
            self._emit_format(*spec)
        return {
            "records": [self.api.render_record(r) for r in self.records],
            "refusals": self.refusals,
            "provenance": self._provenance(),
            "private": dict(sorted(self.private.items())),
        }

    def _collect_namespace(self, ns: str) -> None:
        try:
            mod = importlib.import_module(ns)
        except BaseException as exc:  # noqa: BLE001 -- one namespace's failure is reported, not fatal
            self.refusals.append(f"{ns}: cannot import: {_why(exc)}")
            return
        names = getattr(mod, "__all__", None)
        if names is None:
            if not self.assume_dir:
                self.refusals.append(f"{ns}: has no __all__")
                return
            names = [n for n in vars(mod) if not n.startswith("_")]
        for name in names:
            key = f"{ns}:{name}"
            try:
                obj = getattr(mod, name)
            except BaseException as exc:  # noqa: BLE001 -- a broken lazy export
                self.refusals.append(f"{key}: not bound at runtime: {_why(exc)}")
                continue
            self._add_binding(key, obj, [])

    def _add_binding(self, key: str, obj: object, path: "list[tuple[int, str]]") -> None:
        self.bindings[key] = obj
        self.paths[key] = path
        if not _is_class(obj):
            return
        self.keys_by_id.setdefault(id(obj), []).append(key)
        inner_path = [*path, (id(obj), key)]
        try:
            members = self._members(obj)
        except Exception as exc:  # noqa: BLE001 -- reported when the binding is emitted
            self.refusals.append(f"{key}: cannot enumerate members: {_why(exc)}")
            return
        for name, raw in sorted(members.items()):
            if _is_class(raw) and id(raw) not in {i for i, _ in inner_path}:
                self._add_binding(f"{key}.{name}", raw, inner_path)

    # -- classification ---------------------------------------------------

    @staticmethod
    def _pydantic_model(obj: object) -> bool:
        pydantic = sys.modules.get("pydantic")
        base = getattr(pydantic, "BaseModel", None)
        return base is not None and _is_class(obj) and issubclass(obj, base) and obj is not base

    def _kind(self, obj: object) -> str:
        import typing_extensions

        if issubclass(type(obj), types.ModuleType):
            return "module"
        if _is_alias(obj):
            return "alias"
        if _is_class(obj) and typing_extensions.is_typeddict(obj):
            return "typeddict"
        if _is_class(obj):
            return self._class_kind(obj)
        # A partial is always a callable binding: isroutine() says otherwise per version.
        if _is_routine(obj) and not issubclass(type(obj), functools.partial):
            return "function"
        if callable(obj):
            try:
                inspect.signature(obj)
            # Any reason: `value` is for non-callables only. inspect may format the
            # object's repr (a default's __repr__ included) into its error first;
            # the result is still this refusal.
            except Exception as exc:
                raise _RefuseError("callable without a readable signature") from exc
            return "callable"
        return "value"

    def _class_kind(self, cls: type) -> str:
        if issubclass(cls, enum.Enum):
            return "enum"
        if cls.__dict__.get("_is_protocol", False):
            return "protocol"
        if self._pydantic_model(cls):
            return "model"
        return "class"

    @staticmethod
    def _otto(klass: type) -> bool:
        module = getattr(klass, "__module__", None)
        return isinstance(module, str) and (module == PACKAGE or module.startswith(PACKAGE + "."))

    def _public(self, name: str) -> bool:
        return not name.startswith("_") or name in self.api.SUPPORTED_DUNDERS

    def _field_names(self, cls: type) -> "list[str]":
        import typing_extensions

        names: list[str] = []
        if dataclasses.is_dataclass(cls):
            names += [f.name for f in dataclasses.fields(cls)]
        if self._pydantic_model(cls):
            names += list(cls.model_fields)
        if issubclass(cls, tuple) and isinstance(getattr(cls, "_fields", None), tuple):
            names += list(cls._fields)
        if cls.__dict__.get("_is_protocol", False):
            in_dicts = {n for c in cls.__mro__ if c is not object for n in vars(c)}
            names += sorted(
                n for n in typing_extensions.get_protocol_members(cls) if n not in in_dicts
            )
        return list(dict.fromkeys(names))

    def _members(self, cls: type) -> "dict[str, object]":
        """Return *cls*'s public effective members (dump spec §3.4): name -> raw value.

        A supported dunder set to ``None`` (a dataclass's ``__hash__ = None``) is
        the protocol switched off, so it is absent, not an ``attribute``: gaining
        it later is an addition and losing a real one a removal. It still hides
        the same name further up the MRO, as it does at runtime.
        """
        if not self._otto(cls):  # members of a non-otto class are never enumerated (§3.5)
            return {}
        fields = self._field_names(cls)
        skip = set(fields) | (set(cls.__members__) if issubclass(cls, enum.Enum) else set())
        out: dict[str, object] = {}
        for owner in cls.__mro__:
            if owner is not cls and not self._otto(owner):
                continue
            for name, raw in vars(owner).items():
                if name not in out and name not in skip and self._public(name):
                    out[name] = raw
        out = {
            name: raw
            for name, raw in out.items()
            if raw is not None or name not in self.api.SUPPORTED_DUNDERS
        }
        for name in fields:
            if self._public(name):
                out[name] = _FIELD
        return out

    # -- values and calls -------------------------------------------------

    def _class_set(self, cls: type) -> "str | None":
        keys = self.keys_by_id.get(id(cls))
        return "{" + ",".join(sorted(keys)) + "}" if keys else None

    def _encode(self, value: object) -> str:
        try:
            return self.api.encode_value(value, self._class_set)
        except self.api.UndeclaredEnumError as exc:
            raise _RefuseError(f"enum default {exc}") from exc

    def _call(self, obj: object, drop_receiver: bool) -> "list[str]":
        try:
            sig = inspect.signature(obj)
        except (TypeError, ValueError) as exc:
            raise _RefuseError(f"no readable signature: {_why(exc)}") from exc
        params = self._params(sig)
        if _is_class(obj):
            callkind = "sync"  # a class is sync, whatever its __wrapped__ points at
        else:
            # The params recorded are inspect's; the kind is the target's. They are
            # recorded together only when the target's own signature encodes the
            # same. Encoded tokens are compared, never Signature or default objects,
            # so no default's __eq__ ever runs.
            target, derived = call_target(obj)
            if self._params(derived) != params:
                raise CallKindError(
                    f"a {type(obj).__name__} whose signature is not its call target's"
                )
            callkind = code_kind(target)
        if drop_receiver and params and params[0].kind in ("PO", "PK"):
            params = params[1:]
        return [callkind, self.api.render_params(params)]

    def _params(self, sig: inspect.Signature) -> list:
        """Return *sig*'s parameters as encoded ``Param`` tokens (dump spec §2.4)."""
        return [
            self.api.Param(
                _PARAM_KIND[p.kind],
                p.name,
                None if p.default is inspect.Parameter.empty else self._encode(p.default),
            )
            for p in sig.parameters.values()
        ]

    def _class_call(self, cls: type) -> "list[str]":
        init_owner = next((c for c in cls.__mro__ if "__init__" in vars(c)), object)
        new_owner = next((c for c in cls.__mro__ if "__new__" in vars(c)), object)
        if init_owner.__module__ == "builtins" and new_owner.__module__ == "builtins":
            if init_owner is object and new_owner is object:
                return ["sync", self.api.EMPTY]
            first = next(c for c in cls.__mro__ if c.__module__ == "builtins" and c is not object)
            return ["sync", f"@builtin:builtins.{first.__qualname__}"]
        return self._call(cls, drop_receiver=False)

    # -- emission ---------------------------------------------------------

    def _add(self, kind: str, key: str, fields: "list[str]") -> None:
        self.records.append(self.api.Record(kind, key, fields))

    def _emit(self, key: str, obj: object) -> None:
        """Emit *obj*'s records; a binding that cannot be recorded is a refusal, never a crash."""
        mark = len(self.records)
        try:
            self._emit_binding(key, obj)
        # spec §3.2: any other failure is a refusal; SystemExit too, since recording
        # runs the binding's own code. KeyboardInterrupt still stops the producer.
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            del self.records[mark:]
            self.private.pop(key, None)
            self.refusals.append(f"{key}: cannot be recorded: {_why(exc)}")

    def _emit_binding(self, key: str, obj: object) -> None:
        try:
            kind = self._kind(obj)
        except _RefuseError as exc:
            self.refusals.append(f"{key}: {exc}")
            return
        self._add("name", key, [kind])
        try:
            if kind in ("function", "callable"):
                self._add("call", key, self._call(obj, drop_receiver=False))
            elif kind == "typeddict":
                self._typeddict_inputs(key, obj)
            elif kind in ("class", "model", "protocol", "enum"):
                self._emit_class(key, obj, kind)
        except _RefuseError as exc:
            self.refusals.append(f"{key}: {exc}")

    def _mro(self, cls: type) -> str:
        entries = []
        for base in cls.__mro__[1:]:
            if base is object:
                continue
            if base.__module__ == "builtins":
                entries.append(f"@builtin:builtins.{base.__qualname__}")
                continue
            classes = self._class_set(base)
            if classes is not None:
                entries.append(classes)
        return " ".join(entries) or self.api.EMPTY

    def _names_field(self, key: str, names: "list[str]") -> str:
        hidden = [n for n in names if _is_private(n)]
        if hidden:
            shown = ", ".join(f"{key}.{n}" for n in hidden)
            raise _RefuseError(f"hidden obligation {shown}: give it a public name")
        return " ".join(names) or self.api.EMPTY

    def _emit_class(self, key: str, cls: type, kind: str) -> None:
        # The private map is separate from the records: a refused class (a hidden
        # obligation, say) still reports its underscore members, so the docs
        # validator can judge whether the docs teach their use (dump spec §7.4).
        call_params: list = []
        try:
            self._class_records(key, cls, kind, call_params)
        finally:
            self.private[key] = self._private(cls, call_params)

    def _class_records(self, key: str, cls: type, kind: str, call_params: list) -> None:
        import typing_extensions

        check_metaclass_call(cls)
        self._add("mro", key, [self._mro(cls)])
        if kind == "class":
            call = self._class_call(cls)
            self._add("call", key, call)
            call_params += self.api.parse_call(self.api.Record("call", key, call)).params or []
        if kind == "model":
            self._model_inputs(key, cls)
        for name, raw in sorted(self._members(cls).items()):
            self._try_member(key, name, raw)
        if kind in ("class", "model"):
            abstract = sorted(getattr(cls, "__abstractmethods__", ()))
            self._add("abstract", key, [self._names_field(key, abstract)])
        if kind == "protocol":
            required = sorted(typing_extensions.get_protocol_members(cls))
            self._add("requires", key, [self._names_field(key, required)])
        if kind == "enum":
            for name, member in cls.__members__.items():
                self._enum_member(key, name, member.value)

    def _enum_member(self, key: str, name: str, value: object) -> None:
        try:
            encoded = self._encode(value)
        except _RefuseError as exc:
            self.refusals.append(f"{key}.{name}: {exc}")
            return
        if _opaque(encoded):
            self.refusals.append(f"{key}.{name}: enum member value cannot be encoded")
            return
        self._add("enum", key, [name, encoded])

    def _try_member(self, key: str, name: str, raw: object) -> None:
        try:
            self._member(key, name, raw)
        except _RefuseError as exc:
            self.refusals.append(f"{key}.{name}: {exc}")
        except (Exception, SystemExit) as exc:  # noqa: BLE001 -- as in _emit
            self.refusals.append(f"{key}.{name}: cannot be recorded: {_why(exc)}")

    def _member(self, key: str, name: str, raw: object) -> None:
        mkey = f"{key}.{name}"
        if raw is _FIELD:
            self._add("member", mkey, ["field"])
        elif issubclass(type(raw), staticmethod):
            self._add("member", mkey, ["staticmethod", *self._call(raw.__func__, False)])
        elif issubclass(type(raw), classmethod):
            self._add("member", mkey, ["classmethod", *self._call(raw.__func__, True)])
        elif issubclass(type(raw), property):
            letters = "".join(
                c if f is not None else "-"
                for c, f in zip("gsd", (raw.fget, raw.fset, raw.fdel), strict=True)
            )
            self._add("member", mkey, [f"property:{letters}"])
        elif issubclass(type(raw), functools.cached_property):
            self._add("member", mkey, ["property:g--"])
        elif type(raw) is types.FunctionType:
            self._add("member", mkey, ["method", *self._call(raw, True)])
        elif issubclass(type(raw), functools.partial):  # partial has __get__ only on 3.13+
            self._add("member", mkey, ["attribute"])
        elif type(raw) is types.MemberDescriptorType:  # a __slots__ entry (§3.4)
            self._add("member", mkey, ["field"])
        elif _is_class(raw):
            chain = [*self.paths[key], (id(self.bindings[key]), key)]
            target = next((k for i, k in chain if i == id(raw)), None)
            self._add("member", mkey, [f"classref={target}" if target else "class"])
        elif any(hasattr(type(raw), a) for a in ("__get__", "__set__", "__delete__")):
            raise _RefuseError(f"unclassifiable member ({type(raw).__name__} descriptor)")
        else:
            self._add("member", mkey, ["attribute"])

    def _routes(self, alias: object) -> "set[str]":
        if isinstance(alias, str):
            return {self._encode(alias)}
        if hasattr(alias, "choices"):
            return {r for choice in alias.choices for r in self._routes(choice)}
        if hasattr(alias, "path"):
            return {self._encode(tuple(alias.path))}
        raise _RefuseError(f"unknown validation alias {type(alias).__name__}")

    @staticmethod
    def _validation_routes(config: "typing.Mapping") -> "tuple[bool, bool]":
        """Return whether a model validates a field by its name, and by its alias.

        The order pydantic resolves them in (``ConfigWrapper.core_config``):
        ``populate_by_name`` is the legacy spelling of ``validate_by_name``, read
        only when ``validate_by_name`` is unset, and then it also turns
        ``validate_by_alias`` on; ``validate_by_alias=False`` with
        ``validate_by_name`` unset turns ``validate_by_name`` on.
        """
        by_name = config.get("validate_by_name")
        by_alias = config.get("validate_by_alias")
        populate = config.get("populate_by_name")
        if populate is not None and by_name is None:
            by_alias, by_name = True, populate
        if by_alias is False and by_name is None:
            by_name = True
        return bool(by_name), by_alias is None or bool(by_alias)

    def _model_inputs(self, key: str, cls: type) -> None:
        by_name, by_alias = self._validation_routes(getattr(cls, "model_config", {}) or {})
        for name, info in cls.model_fields.items():
            alias = info.validation_alias if info.validation_alias is not None else info.alias
            routes: set[str] = set()
            if alias is None:
                routes.add(self._encode(name))
            else:
                if by_alias:
                    routes |= self._routes(alias)
                if by_name:
                    routes.add(self._encode(name))
            if info.is_required():
                required, default = "required", self.api.EMPTY
            elif info.default_factory is not None:
                required, default = "optional", "O"
            else:
                required, default = "optional", self._encode(info.default)
            self._add(
                "input",
                key,
                [self._encode(name), required, default, " ".join(sorted(routes)) or self.api.EMPTY],
            )

    def _typeddict_inputs(self, key: str, cls: type) -> None:
        keys = [(k, "required") for k in cls.__required_keys__]
        keys += [(k, "optional") for k in cls.__optional_keys__]
        for name, required in sorted(keys):
            encoded = self._encode(name)
            self._add("input", key, [encoded, required, self.api.EMPTY, encoded])

    def _private(self, cls: type, call_params: list) -> "list[str]":
        names = {n for n in self._field_names(cls) if _is_private(n)}
        names |= {p.name for p in call_params if _is_private(p.name)}
        for owner in cls.__mro__:
            if self._otto(owner):
                names |= {n for n in vars(owner) if _is_private(n)}
        return sorted(names)

    # -- formats (dump spec §13) ------------------------------------------

    def _emit_format(self, name: str, reads: str, writes: str) -> None:
        """Emit one ``format`` record; anything it cannot record is a refusal, never a crash."""
        try:
            self._format(name, reads, writes)
        except Exception as exc:  # noqa: BLE001 -- spec §13.2: a refusal, not a crash
            self.refusals.append(f"format {name}: cannot be recorded: {_why(exc)}")

    def _format(self, name: str, reads: str, writes: str) -> None:
        lists = []
        for pointer in (reads, writes):
            if pointer == self.api.EMPTY:
                lists.append([])
                continue
            versions = self._format_versions(name, pointer)
            if versions is None:
                return
            lists.append(versions)
        if not lists[0] and not lists[1]:
            self.refusals.append(f"format {name}: declares no version")
            return
        self._add(self.api.FORMAT, name, [" ".join(v) or self.api.EMPTY for v in lists])

    def _format_versions(self, name: str, pointer: str) -> "list[str] | None":
        module_name, _, const = pointer.partition(":")
        try:
            module = importlib.import_module(module_name)
        except BaseException as exc:  # noqa: BLE001 -- reported, never fatal
            self.refusals.append(f"format {name}: {module_name} does not import: {_why(exc)}")
            return None
        if not hasattr(module, const):
            self.refusals.append(f"format {name}: {pointer} does not exist")
            return None
        value = getattr(module, const)
        literal = _literal_list(module, const)
        if literal is None or type(value) is not list or value != literal:
            self.refusals.append(
                f"format {name}: {pointer} is not a literal list: "
                "the constant must be a literal list of int/str assigned in the module the "
                "pointer names (a re-export or a computed value is not a declaration)"
            )
            return None
        bad = [v for v in value if type(v) not in (int, str)]
        if bad:
            self.refusals.append(
                f"format {name}: {pointer} holds {bad[0]!r}: a version is an int or a str"
            )
            return None
        encoded = [self._encode(v) for v in value]
        if len(set(encoded)) != len(encoded):
            self.refusals.append(f"format {name}: {pointer} has a duplicate version")
            return None
        if not encoded:
            self.refusals.append(f"format {name}: {pointer} declares no version")
            return None
        return sorted(encoded)

    # -- provenance -------------------------------------------------------

    def _provenance(self) -> "list[str]":
        problems = []
        root = self.src + os.sep
        for name, mod in sorted(sys.modules.items()):
            if mod is None or not (name == PACKAGE or name.startswith(PACKAGE + ".")):
                continue
            places = [getattr(mod, "__file__", None), *(getattr(mod, "__path__", None) or [])]
            places = [p for p in places if isinstance(p, str)]
            if not places:
                problems.append(f"{name}: no __file__ or __path__ proves where it came from")
            problems.extend(
                f"{name}: loaded from {place}, outside {self.src}"
                for place in places
                if not os.path.realpath(place).startswith(root)
            )
        return problems


def main(argv: "list[str]") -> int:
    """Run the producer on the command line *argv*; print the JSON report last."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--src", required=True)
    parser.add_argument("--assume-dir", action="store_true")
    parser.add_argument(
        "--format",
        nargs=3,
        action="append",
        default=[],
        metavar=("NAME", "READS", "WRITES"),
    )
    parser.add_argument("namespaces", nargs="*")
    args = parser.parse_args(argv)
    sys.path.insert(0, args.src)
    import api_records

    report = Producer(api_records, args.src, args.assume_dir).run(args.namespaces, args.format)
    sys.__stdout__.write(json.dumps(report) + "\n")
    sys.__stdout__.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
