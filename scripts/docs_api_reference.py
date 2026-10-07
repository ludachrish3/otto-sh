"""Render otto's API reference in two halves: Public API and Internals.

``docs/conf.py`` connects the hooks below. Spec 1 §6 "API reference" gives
each declared namespace a page that renders its ``__all__``, and gives every
other module an Internals page with ``:ignore-module-all:``. These rules keep
that split building under ``-W``:

* **One documented home per public object.** An object several declared
  namespaces hold (``otto:CommandResult``, ``otto.host:CommandResult``,
  ``otto.result:CommandResult``) is indexed once, at the home
  :func:`choose_home` picks. Each other holder lists it under "Also exported
  here", and an Internals page leaves it out (:func:`skip_member`). Two
  indexed targets behind one short name fail the build with "more than one
  target found".
* **Marks** (``docs/api/stability.md``). A public module opens with a banner
  naming its tier, unless the tier is ``stable``, which is never marked. An
  Internals module opens with an "internal" note. The announcement bar carries
  a notice while otto is before 1.0 (:func:`provisional_notice`).
* **Links keep resolving.** A reference written against a defining module
  (``otto.host.factory.create_host_from_dict``) resolves to the object's home
  (:func:`public_target`). A relative role in a docstring that is documented
  away from its module is spelled out against that module
  (:func:`qualify_relative_roles`).

``scripts/check_docs_api_marks.py`` checks the BUILT site against the same
declaration, so a hook that stops marking fails there.
"""

import dataclasses
import importlib
import inspect
import re
import sys
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from scripts.api_manifest import Namespace, load_manifest  # noqa: E402 -- path set up above

#: The namespace declaration the reference follows.
MANIFEST = REPO_ROOT / "api" / "public.toml"

_IMMUTABLE = (int, float, complex, str, bytes, bool, type(None), tuple, frozenset)
_ROLE = re.compile(r"(:(?:py:)?(?:class|func|meth|attr|data|exc|obj):)`([^`]+)`")
_TITLED = re.compile(r"(?P<title>.*?)\s*<(?P<target>[^<>]+)>", re.DOTALL)
_DOTTED = re.compile(r"[A-Za-z_]\w*(?:\.\w+)*")
#: A lazy-table entry that names ``(module, attribute)`` has this many parts.
_MODULE_AND_NAME = 2


@dataclass(frozen=True)
class Home:
    """Where one public object is documented: on *namespace*'s page, as *name*."""

    namespace: str
    name: str

    @property
    def path(self) -> str:
        """The dotted target the object is indexed under."""
        return f"{self.namespace}.{self.name}"


@dataclass
class Reference:
    """Every public object's home, built once per docs build by :func:`build_reference`."""

    namespaces: "dict[str, Namespace]"
    homes: "dict[int, Home]" = field(default_factory=dict)
    reexports: "dict[str, dict[str, str]]" = field(default_factory=dict)
    objects: "dict[int, object]" = field(default_factory=dict)


def load_namespaces(path: Path = MANIFEST) -> "dict[str, Namespace]":
    """Read the declared namespaces the reference follows."""
    return load_manifest(path)


def trackable(obj: object) -> bool:
    """Whether *obj* can be told apart by identity.

    A value of an immutable type can be shared by unrelated names (``30``,
    ``"utf-8"``), and a builtin class used as an alias (``OsType = str``) IS
    the builtin. Neither gets a single home: it is documented wherever it
    appears. A module is never a documented member.
    """
    if isinstance(obj, (types.ModuleType, *_IMMUTABLE)):
        return False
    return not (type(obj) is type and obj.__module__ == "builtins")


def _code_object(obj: object) -> object:
    """Return the class or routine behind *obj*, or None for a plain value."""
    if isinstance(obj, property):
        obj = obj.fget
    obj = getattr(obj, "__func__", obj)
    return obj if inspect.isclass(obj) or inspect.isroutine(obj) else None


def defining_module(obj: object) -> "str | None":
    """Return the module whose code defines *obj*, or None for a value with no code."""
    code = _code_object(obj)
    module = getattr(code, "__module__", None) if code is not None else None
    return module if isinstance(module, str) else None


def choose_home(holders: "list[str]", defining_module: "str | None") -> str:
    """Return the namespace that documents an object every one of *holders* exports.

    The package root re-exports for convenience, so it is home only to what
    nothing else holds. Among the other holders, one that contains the defining
    module wins (``otto.result`` over ``otto.host`` for ``CommandResult``), then
    the deepest (``otto.host.transfer`` over ``otto.host``), then the first by
    name, so the choice never depends on dict order.
    """
    deep = [h for h in holders if h != "otto"]
    if not deep:
        return "otto"
    if defining_module:
        above = [h for h in deep if defining_module == h or defining_module.startswith(h + ".")]
        deep = above or deep
    return min(deep, key=lambda h: (-h.count("."), h))


def build_reference(namespaces: "dict[str, Namespace]") -> Reference:
    """Import every declared namespace and give each public object one home."""
    held: dict[int, dict[str, str]] = {}
    ref = Reference(namespaces)
    for namespace in sorted(namespaces):
        module = importlib.import_module(namespace)
        for name in getattr(module, "__all__", []):
            obj = getattr(module, name)
            if trackable(obj):
                held.setdefault(id(obj), {}).setdefault(namespace, name)
                ref.objects[id(obj)] = obj
    for key, names in held.items():
        home_ns = choose_home(list(names), defining_module(ref.objects[key]))
        home = Home(home_ns, names[home_ns])
        ref.homes[key] = home
        for namespace, name in names.items():
            if namespace != home_ns:
                ref.reexports.setdefault(namespace, {})[name] = home.path
    return ref


def skip_member(
    ref: Reference,
    documenting: "str | None",
    what: str,
    obj: object,
    options: "Mapping[str, object]",
) -> "bool | None":
    """Return the ``autodoc-skip-member`` verdict for a module member, or None to defer.

    An Internals entry (``:ignore-module-all:``) leaves out every public object.
    A public page leaves out the objects whose home is another namespace.
    """
    if what != "module" or not trackable(obj):
        return None
    home = ref.homes.get(id(obj))
    if home is None:
        return None
    if options.get("ignore-module-all"):
        return True
    return True if home.namespace != documenting else None


def stability_banner(namespace: str, stability: str) -> "list[str]":
    """Return the reST banner a public module opens with. Stable is never marked."""
    if stability == "stable":
        return []
    return [
        f".. admonition:: {stability.capitalize()}",
        f"   :class: otto-stability otto-stability-{stability}",
        "",
        f"   Every public name in ``{namespace}`` is {stability}: it may change in a",
        "   release, and every breaking change is marked in the changelog.",
        "   See :doc:`/api/stability`.",
        "",
    ]


def internal_note(module: str) -> "list[str]":
    """Return the reST note an Internals module opens with."""
    return [
        ".. admonition:: Internal",
        "   :class: otto-internal",
        "",
        f"   ``{module}`` is internal. It is documented for contributors and for",
        "   the guides' links, with no compatibility promise: import from the",
        "   :doc:`Public API </api/index>` instead.",
        "",
    ]


def reexport_lines(ref: Reference, namespace: str) -> "list[str]":
    """Return the "Also exported here" list: what *namespace* holds but does not document."""
    names = ref.reexports.get(namespace, {})
    if not names:
        return []
    lines = ["", ".. rubric:: Also exported here", ""]
    lines += [f"* :py:obj:`{name} <{path}>`" for name, path in sorted(names.items())]
    return lines


def module_docstring(
    ref: Reference, name: str, options: "dict[str, object]", lines: "list[str]"
) -> None:
    """Mark a module docstring in place for the half of the reference it renders in."""
    if options.get("ignore-module-all"):
        lines[:0] = internal_note(name)
        return
    namespace = ref.namespaces.get(name)
    if namespace is None:
        return
    lines[:0] = stability_banner(name, namespace.stability)
    lines.extend(reexport_lines(ref, name))


def qualify_relative_roles(lines: "list[str]", documented_in: str, defined_in: str) -> "list[str]":
    """Spell out each relative role target that only *defined_in* binds.

    A re-exported object is documented at its home, but its docstring's
    relative roles were written against its defining module: Python finds
    ``AppShellTimeoutError`` in ``otto.host.app_shell``, while Sphinx would
    look in the documenting namespace. A target is rewritten to the defining
    path of the otto class or routine it names, or, for a ``:data:`` role, to
    the constant in *defined_in*; the text the reader sees is kept. A target
    the documenting namespace binds, an absolute one, a member name and a
    non-otto import are left alone.
    """
    if documented_in == defined_in or not defined_in.startswith("otto"):
        return list(lines)
    try:
        here = importlib.import_module(documented_in)
        origin = importlib.import_module(defined_in)
    except ImportError:
        return list(lines)

    def spell_out(match: "re.Match[str]") -> str:
        role, body = match.group(1), match.group(2)
        titled = _TITLED.fullmatch(body)
        title = titled.group("title") if titled else None
        target = titled.group("target") if titled else body
        bare = target.lstrip("~")
        if bare.startswith("otto.") or not _DOTTED.fullmatch(bare):
            return match.group(0)
        first, _, rest = bare.partition(".")
        if hasattr(here, first) or not hasattr(origin, first):
            return match.group(0)
        value = getattr(origin, first)
        code = _code_object(value)
        if code is None:
            # Module data has no code to name its module: a :data: role names a
            # constant documented where the docstring's own module binds it.
            # Any other role on a plain value (a typing alias) is left alone.
            if not role.endswith(("data:", "obj:")) or isinstance(value, types.ModuleType):
                return match.group(0)
            module, qualname = defined_in, first
        else:
            module = getattr(code, "__module__", None)
            qualname = getattr(code, "__qualname__", first)
        if not isinstance(module, str) or not module.startswith("otto"):
            return match.group(0)
        full = f"{module}.{qualname}" + (f".{rest}" if rest else "")
        if title is not None:
            shown = title
        elif target.startswith("~"):
            shown = bare.rsplit(".", 1)[-1]
        else:
            shown = bare
        return f"{role}`{shown} <{full}>`"

    return [_ROLE.sub(spell_out, line) for line in lines]


def data_sources(namespace: str, name: str) -> "list[str]":
    """Return the modules whose source may carry the attribute docstring of *namespace*'s *name*.

    Autodoc renders module data only with an attribute docstring in the
    documented module's own source, and a lazy package's ``__init__`` binds
    none: the constant is bound where it is defined. The package's lazy table
    names that module first; then come the loaded ``otto`` modules that bind
    the same object under the same name, the package's own submodules first.
    """
    package = importlib.import_module(namespace)
    if not hasattr(package, name):
        return []
    value = getattr(package, name)
    out: list[str] = []
    for table in ("_LAZY_ATTRS", "_LAZY_EXPORTS"):
        entry = getattr(package, table, {}).get(name)
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, (tuple, list)) and len(entry) == _MODULE_AND_NAME:
            out.append(entry[0])
    bound = [
        module_name
        for module_name, module in sorted(sys.modules.items())
        if module_name.startswith("otto.")
        and module_name != namespace
        and getattr(module, name, None) is value
    ]
    bound.sort(key=lambda m: not m.startswith(namespace + "."))
    return out + [m for m in bound if m not in out]


_DEFAULT_REPR = re.compile(r"<[\w.]+ object at 0x[0-9a-fA-F]+>")
_ANNOTATED_HEAD = re.compile(r"~?(?:\w+\.)*Annotated$")
_OPENERS, _CLOSERS = "[(", "])"


def _top_level_split(text: str) -> "list[str]":
    """Split *text* on the commas outside any bracket or parenthesis."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        depth += (ch in _OPENERS) - (ch in _CLOSERS)
        if ch == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return parts


def _enclosing_annotated(text: str, at: int) -> "tuple[int, int, int] | None":
    """Return ``(head, open, close)`` of the ``Annotated[...]`` whose arguments hold index *at*.

    *head* is where its name starts (``~typing.Annotated``), *open* and *close*
    are its brackets. None when *at* is not directly inside an ``Annotated``.
    """
    depth = 0
    for opening in range(at - 1, -1, -1):
        ch = text[opening]
        if ch in _CLOSERS:
            depth += 1
        elif ch in _OPENERS:
            if depth == 0:
                break
            depth -= 1
    else:
        return None
    head = _ANNOTATED_HEAD.search(text, 0, opening)
    if text[opening] != "[" or head is None:
        return None
    depth = 0
    for close in range(at, len(text)):
        ch = text[close]
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            if depth == 0:
                return (head.start(), opening, close) if ch == "]" else None
            depth -= 1
    return None


def drop_annotated_marker(text: str, marker: str) -> str:
    """Render each ``Annotated[T, <marker>]`` in signature *text* as ``T``.

    *marker* is the metadata object's repr as autodoc writes it into a
    signature. The reference uses it for ``otto.utils.Exclude``, CLI-only
    metadata whose default repr would print a private class and a memory
    address: a Python caller passes a plain ``T``. Other metadata on the same
    ``Annotated`` stays, and an ``Annotated`` without *marker* is untouched.
    Every occurrence is rewritten, nested ones included.
    """
    start = 0
    while (at := text.find(marker, start)) != -1:
        span = _enclosing_annotated(text, at)
        if span is None:
            start = at + len(marker)
            continue
        head, opening, close = span
        args = _top_level_split(text[opening + 1 : close])
        kept = [arg for arg in args[1:] if arg != marker]
        name = text[head : opening + 1]
        inner = f"{name}{', '.join([args[0], *kept])}]" if kept else args[0]
        text = text[:head] + inner + text[close + 1 :]
        start = head
    return text


def drop_annotated_markers(text: str, markers: "list[str]") -> str:
    """Render each ``Annotated[T, <marker>]`` in *text* as ``T``, for every one of *markers*.

    :func:`drop_annotated_marker` for several markers at once, whose text may
    hold brackets and commas of its own: an ``otto.utils.Opt`` renders its help
    string unquoted (``help=Octal [bits], e.g. 755``), which would split the
    ``Annotated`` it sits in at the wrong comma. Each marker stands in as one
    opaque token while its ``Annotated`` is rewritten, and any marker left
    outside an ``Annotated`` is put back as it was.
    """
    tokens: dict[str, str] = {}
    for i, marker in enumerate(sorted(set(markers), key=len, reverse=True)):
        if marker and marker in text:
            token = f"\x00{i}\x00"
            tokens[token] = marker
            text = drop_annotated_marker(text.replace(marker, token), token)
    for token, marker in tokens.items():
        text = text.replace(token, marker)
    return text


def annotated_metadata(annotations: "list[object]") -> "list[object]":
    """Return the metadata of every ``Annotated`` in *annotations*, nested ones included."""
    out: list[object] = []
    pending = list(annotations)
    while pending:
        annotation = pending.pop()
        if isinstance(annotation, (list, tuple)):
            pending.extend(annotation)
            continue
        out.extend(getattr(annotation, "__metadata__", ()))
        pending.extend(typing.get_args(annotation))
    return out


def annotated_metadata_text(meta: object) -> "list[str]":
    """Return *meta* as autodoc writes it inside an ``Annotated[...]``, in each typehint format.

    Sphinx's own stringifier renders it, so the text matches the signature
    autodoc hands a hook whichever ``autodoc_typehints_format`` is set.
    """
    from sphinx.util.typing import stringify_annotation

    out: list[str] = []
    for mode in ("smart", "fully-qualified-except-typing"):
        text = stringify_annotation(typing.Annotated[int, meta], mode)
        out.append(text[text.index("[int, ") + len("[int, ") : -1])
    return out


def _parameter_spans(text: str) -> "list[tuple[int, int]]":
    """Return the ``(start, end)`` of each top-level comma-separated part of *text*.

    Unlike :func:`_top_level_split`, a comma or bracket inside a quoted string
    (a default such as ``'a,b'`` or ``'['``) neither splits nor nests.
    """
    spans: list[tuple[int, int]] = []
    depth, start, quote, i = 0, 0, "", 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            depth -= 1
        elif ch == "," and depth == 0:
            spans.append((start, i))
            start = i + 1
        i += 1
    spans.append((start, len(text)))
    return spans


def replace_defaults(signature: str, defaults: "Mapping[str, tuple[str, str]]") -> str:
    """Rewrite the defaults of the parameters *defaults* names in *signature*.

    *defaults* maps a parameter's name to its default as the signature shows it
    and the text to show instead. A parameter whose default reads otherwise is
    left as it is, so a stale entry cannot rewrite the wrong value. Each new
    default is spliced in where the old one stood: every other character of
    *signature* is kept as it was.
    """
    if not defaults or not (signature.startswith("(") and signature.endswith(")")):
        return signature
    inner = signature[1:-1]
    for start, end in reversed(_parameter_spans(inner)):
        part = inner[start:end]
        name = part.split(":", 1)[0].split("=", 1)[0].strip().lstrip("*")
        if name not in defaults:
            continue
        shown, valid = defaults[name]
        stripped = part.rstrip()
        if any(stripped.endswith(separator + shown) for separator in (" = ", "=")):
            at = start + len(stripped) - len(shown)
            inner = inner[:at] + valid + inner[at + len(shown) :]
    return f"({inner})"


#: Builtin containers whose call with no arguments builds the empty default,
#: though ``inspect.signature`` cannot read some of them (``dict``, ``set``).
_EMPTY_CONTAINERS = [list, dict, set, frozenset, tuple]


def _python_name(obj: object) -> "str | None":
    """Return the qualified name a caller writes for *obj* (``Outer.Inner``), or None.

    A lambda, or anything defined inside a function (``<locals>``), has none.
    """
    qualname = getattr(obj, "__qualname__", None)
    if isinstance(qualname, str) and all(p.isidentifier() for p in qualname.split(".")):
        return qualname
    return None


def _callable_without_arguments(factory: type) -> bool:
    """Whether calling *factory* with no arguments is valid, as its signature reads."""
    if factory in _EMPTY_CONTAINERS:
        return True
    try:
        params = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):
        return False
    variadic = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    return all(p.default is not p.empty or p.kind in variadic for p in params)


def factory_default(factory: object) -> str:
    """Return the Python a signature shows for a field built by *factory*.

    A dataclass or pydantic field with a ``default_factory`` shows ``<factory>``,
    which is not Python, so Sphinx cannot parse the signature it sits in. A
    class whose signature takes no required argument (``list``, ``LabInfo``)
    builds the default when called bare, so ``list()`` states it exactly. Any
    other factory shows as ``...``: a default exists, and the field's docs say
    what it is. That covers a lambda or a function, a class defined where a
    reader cannot name it, a class whose signature cannot be read, and a class
    that requires an argument (pydantic passes a one-argument factory the
    validated data, so a bare call would misstate the default). A
    parameterized generic (``dict[str, Any]``) is judged by its class: calling
    it builds that class, so it shows ``dict()``.
    """
    factory = typing.get_origin(factory) or factory
    if isinstance(factory, type) and _callable_without_arguments(factory):
        name = _python_name(factory)
        if name is not None:
            return f"{name}()"
    return "..."


def object_default(value: object) -> "str | None":
    """Return the Python a signature shows for a class or function default, or None.

    Sphinx prints such a default as its repr (``<class 'otto.host.unix_host.UnixHost'>``,
    ``<function SessionManager.<lambda>>``), which is not Python. A class or a
    named function shows as the qualified name a caller would write
    (``UnixHost``, ``Outer.Inner``); a lambda, or anything defined inside a
    function, has none, so it shows as ``...``. Any other value keeps its repr.
    """
    if not (isinstance(value, type) or inspect.isroutine(value)):
        return None
    return _python_name(value) or "..."


def python_defaults(
    params: "Mapping[str, inspect.Parameter]", factories: "Mapping[str, object]"
) -> "dict[str, tuple[str, str]]":
    """Map each parameter whose default Sphinx would show as non-Python to that text and Python.

    *params* is the signature's parameters, *factories* the ``default_factory``
    of each field (:func:`field_factories`). The result is what
    :func:`replace_defaults` takes: a ``<factory>`` default becomes
    :func:`factory_default`'s text, a class or function default
    :func:`object_default`'s, and every other default is left out.
    """
    from sphinx.util.inspect import object_description

    out: dict[str, tuple[str, str]] = {}
    for name, param in params.items():
        if param.default is param.empty:
            continue
        shown = object_description(param.default)
        if shown == "<factory>":
            out[name] = (shown, factory_default(factories.get(name)))
        elif (valid := object_default(param.default)) is not None:
            out[name] = (shown, valid)
    return out


def field_factories(cls: object) -> "dict[str, object]":
    """Return the ``default_factory`` of each field of the dataclass or pydantic class *cls*."""
    pydantic_fields = getattr(cls, "__pydantic_fields__", None)
    if isinstance(pydantic_fields, Mapping):
        return {
            name: info.default_factory
            for name, info in pydantic_fields.items()
            if getattr(info, "default_factory", None) is not None
        }
    if isinstance(cls, type) and dataclasses.is_dataclass(cls):
        return {
            f.name: f.default_factory
            for f in dataclasses.fields(cls)
            if f.default_factory is not dataclasses.MISSING
        }
    return {}


def shows_value(obj: object) -> bool:
    """Whether the reference shows *obj*'s value beside its name.

    A value whose type keeps ``object``'s default repr (a registry, an app)
    would render as ``<otto.registry.Registry object>``, which says nothing the
    name and docstring do not. So would a container that holds one (a dict of
    parser instances), and a value whose repr fails cannot be rendered at all.
    """
    if type(obj).__repr__ is object.__repr__:
        return False
    try:
        text = repr(obj)
    except Exception:  # noqa: BLE001 -- any failing repr is a value the page cannot show
        return False
    return _DEFAULT_REPR.search(text) is None


def documenting_module(path: str) -> "str | None":
    """Return the module an autodoc entry named *path* is documented under.

    Autodoc names each entry by the path its directive gave
    (``otto.coverage.CoverageReporter.report``); the longest importable prefix
    is the page's module. ``env.temp_data["autodoc:module"]`` cannot stand in:
    autodoc resets it to None after each class's members.
    """
    found = _locate(path)
    return found[0] if found is not None else None


def owner_module(path: str) -> "str | None":
    """Return the defining module of the class that holds the attribute at *path*.

    An attribute's value (a field default, a constant) carries no module of its
    own, so its docstring's relative roles are read against the class's.
    """
    owner, _, _ = path.rpartition(".")
    found = _locate(owner) if owner else None
    if found is None:
        return None
    _, holder, name, rest = found
    obj = getattr(holder, name)
    for part in rest.split(".")[1:]:
        obj = getattr(obj, part, None)
    return defining_module(obj)


def _locate(dotted: str) -> "tuple[str, object, str, str] | None":
    """Split *dotted* into its longest importable module, the attribute after it, and the rest."""
    parts = dotted.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:cut])
        module = sys.modules.get(module_name)
        if module is None:
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
        if not hasattr(module, parts[cut]):
            return None
        rest = ".".join(parts[cut + 1 :])
        return module_name, module, parts[cut], f".{rest}" if rest else ""
    return None


def _origin_path(owner: object, name: str, obj: object) -> "str | None":
    """Return where an object with no public home is documented: its defining module's path."""
    code = _code_object(obj)
    module = getattr(code, "__module__", None) if code is not None else None
    if isinstance(module, str) and module.startswith("otto"):
        return f"{module}.{getattr(code, '__qualname__', name)}"
    for table in ("_LAZY_ATTRS", "_LAZY_EXPORTS"):
        entry = getattr(owner, table, {}).get(name)
        if isinstance(entry, str):
            return f"{entry}.{name}"
        if isinstance(entry, (tuple, list)) and len(entry) == _MODULE_AND_NAME:
            return f"{entry[0]}.{entry[1]}"
    return None


def public_target(ref: Reference, target: str, context_module: "str | None" = None) -> "str | None":
    """Return the documented path for a reference that names an object by another path.

    A public object resolves to its home. Any other otto object resolves to its
    defining module, which is where its Internals page documents it. Returns
    None when *target* already is that path, or names nothing importable.
    """
    bare = target.lstrip("~.")
    candidates = [bare] if bare.startswith("otto.") else []
    if context_module and not bare.startswith("otto."):
        candidates.append(f"{context_module}.{bare}")
    for candidate in candidates:
        found = _locate(candidate)
        if found is None:
            continue
        module_name, owner, name, rest = found
        obj = getattr(owner, name)
        home = ref.homes.get(id(obj)) if trackable(obj) else None
        path = home.path if home is not None else _origin_path(owner, name, obj)
        if path is not None and path != f"{module_name}.{name}":
            return path + rest
    return None


def provisional_notice(release: str) -> "str | None":
    """Return the site-wide notice while *release* is before 1.0, else None."""
    major = release.split(".", 1)[0]
    if not major.isdigit() or int(major) >= 1:
        return None
    return (
        "otto's Python API is provisional until 1.0: a public name may change in "
        "a release, and every breaking change is marked in the changelog."
    )
