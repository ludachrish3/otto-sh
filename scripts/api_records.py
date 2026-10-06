r"""The API dump's grammar: the one reader and writer of golden schema v2.

Spec: ``docs/superpowers/specs/2026-10-05-api-dump-design.md`` §2 (format) and
§3 (records). Standard library only: ``scripts/api_dump_child.py`` imports this
module from inside a commit archive's dependency-only environment.

A dump is ``# api-snapshot v2``, then ``# producer-schema <n>``, then one
record per line: tab-separated ``<kind>\t<key>[\t<field>...]``. A field that
holds a list separates its elements with single spaces and writes an empty
list as ``-``; no encoded value contains a space, tab or newline.

A ``format`` record (dump spec §13.2) is ``format\t<name>\t<reads>\t<writes>``:
each list holds ``I:``/``S:`` versions sorted by encoded text, and the two lists
are never both empty. It belongs to no binding and sorts after every binding.
"""

import enum
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass

V2_HEADER = "# api-snapshot v2"
SCHEMA_PREFIX = "# producer-schema "
PRODUCER_SCHEMA = 1
EMPTY = "-"
FORMAT = "format"
KIND_ORDER = ["name", "mro", "call", "input", "member", "abstract", "requires", "enum", FORMAT]
BINDING_KINDS = frozenset(
    {
        "module",
        "alias",
        "typeddict",
        "enum",
        "protocol",
        "model",
        "class",
        "function",
        "callable",
        "value",
    }
)
CALL_KINDS = frozenset({"sync", "coroutine", "asyncgen"})
SUPPORTED_DUNDERS = frozenset(
    {
        "__call__",
        "__enter__",
        "__exit__",
        "__aenter__",
        "__aexit__",
        "__iter__",
        "__next__",
        "__aiter__",
        "__anext__",
        "__len__",
        "__contains__",
        "__getitem__",
        "__setitem__",
        "__delitem__",
        "__eq__",
        "__hash__",
    }
)
# Which records a binding kind must carry, and which it may carry (dump spec §2.3).
REQUIRED = {
    "function": {"call"},
    "callable": {"call"},
    "class": {"mro", "call", "abstract"},
    "model": {"mro", "abstract"},
    "protocol": {"mro", "requires"},
    "enum": {"mro"},
}
ALLOWED = {
    "function": {"name", "call"},
    "callable": {"name", "call"},
    "class": {"name", "mro", "call", "member", "abstract"},
    "model": {"name", "mro", "input", "member", "abstract"},
    "protocol": {"name", "mro", "member", "requires"},
    "enum": {"name", "mro", "member", "enum"},
    "typeddict": {"name", "input"},
    "module": {"name"},
    "alias": {"name"},
    "value": {"name"},
}
_PARAM = re.compile(r"(PO|PK|VP|KO|VK):([^:\s]+):(.+)")
_BUILTIN = re.compile(r"@builtin:builtins\.\w+")
_CLASS_SET = re.compile(r"\{[^{}\s,]+(?:,[^{}\s,]+)*\}")
_ENUM_VALUE = re.compile(r"E:(\{[^{}\s]+\}):([^:=\s]+)=(.+)")
_FLOAT = re.compile(r"F:(?:nan|inf|-inf|-?0x[0-9a-f]\.[0-9a-f]+p[+-][0-9]+)")
_PROPERTY = re.compile(r"property:[g-][s-][d-]")
_WHITESPACE = re.compile(r"[ \t\n\r\f\v]")
_FORMAT_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_INT = re.compile(r"I:(?:0|-?[1-9][0-9]*)")
_VERSION = re.compile(r"I:(?:0|[1-9][0-9]*)|S:.+")


def _is_name(text: str) -> bool:
    """Return True when *text* is a name the grammar accepts: what ``str.isidentifier()`` accepts.

    Python's own rule, so every Unicode identifier the producer can read off a
    module is one the parser takes back; it never holds whitespace or a separator.
    """
    return text.isidentifier()


def _is_key(text: str) -> bool:
    """Return True when *text* is a binding key: ``otto[.<name>]...:<name>[.<name>]...``."""
    module, colon, path = text.partition(":")
    head, *rest = module.split(".")
    return (
        colon == ":"
        and head == "otto"
        and all(_is_name(part) for part in rest)
        and all(_is_name(part) for part in path.split("."))
    )


class DumpError(ValueError):
    """A dump that does not parse, or breaks a structural rule (dump spec §2.3)."""


class UndeclaredEnumError(ValueError):
    """An enum used as a value whose class has no public binding (dump spec §2.4)."""

    def __init__(self, cls: type) -> None:
        super().__init__(f"{cls.__module__}.{cls.__qualname__}: declare the enum")
        self.cls = cls


def _escape(text: str) -> str:
    return text.replace(" ", "\\u0020")


def _json(value: object) -> str:
    return _escape(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def _encode_float(value: float) -> str:
    raw = float.__float__(value)  # float's own conversion: a subclass's __float__ never runs
    if math.isnan(raw):
        return "F:nan"
    if math.isinf(raw):
        return "F:inf" if raw > 0 else "F:-inf"
    return f"F:{raw.hex()}"


def _encode_scalar(value: object) -> "str | None":
    """Encode the scalar types, or return None for a container or an untracked value.

    Dispatch reads the value's REAL type, never ``isinstance``, which trusts a
    ``__class__`` the value may claim; and each conversion calls the base type's
    own method, so no ``__bool__``, ``__int__``, ``__str__`` or other hook of the
    value runs (dump spec §2.4).
    """
    tp = type(value)
    if value is None:
        return "N"
    if tp is bool:  # bool cannot be subclassed
        return "B:true" if value is True else "B:false"
    if issubclass(tp, int):
        return f"I:{int.__repr__(value)}"
    if issubclass(tp, float):
        return _encode_float(value)
    if issubclass(tp, str):
        return f"S:{_json(str.__str__(value))}"
    if issubclass(tp, bytes):
        return f"Y:{bytes.hex(value)}"
    return None


def encode_value(value: object, class_set_of: "Callable[[type], str | None]") -> str:
    """Encode *value* with a type tag (dump spec §2.4); ``O`` when it is not tracked.

    *class_set_of* maps an enum class to its ``{binding,...}`` set, or None when
    it has no public binding, which raises :class:`UndeclaredEnumError`. An enum
    is tested before the scalars, since an ``IntEnum`` member is also an int.
    """
    tp = type(value)
    if issubclass(tp, enum.Enum):
        classes = class_set_of(tp)
        if classes is None:
            raise UndeclaredEnumError(tp)
        name = next((n for n, m in tp.__members__.items() if m is value), "*")
        return f"E:{classes}:{name}={encode_value(value.value, class_set_of)}"
    scalar = _encode_scalar(value)
    if scalar is not None:
        return scalar
    if issubclass(tp, tuple):
        return f"T:{_json([encode_value(v, class_set_of) for v in tuple.__iter__(value)])}"
    if issubclass(tp, frozenset):
        items = frozenset.__iter__(value)
        return f"Z:{_json(sorted(encode_value(v, class_set_of) for v in items))}"
    return "O"


def _check_value(text: str) -> None:
    """Raise :class:`DumpError` unless *text* is a well-formed encoded value."""
    if _WHITESPACE.search(text):
        raise DumpError(f"an encoded value holds whitespace: {text!r}")
    ok = text in ("N", "O", "B:true", "B:false") or bool(
        _INT.fullmatch(text) or _FLOAT.fullmatch(text) or re.fullmatch(r"Y:[0-9a-f]*", text)
    )
    if not ok and text.startswith("S:"):
        try:
            ok = isinstance(json.loads(text[2:]), str)
        except ValueError:
            ok = False
    if not ok and text[:2] in ("T:", "Z:"):
        try:
            items = json.loads(text[2:])
        except ValueError:
            items = None
        if isinstance(items, list) and all(isinstance(i, str) for i in items):
            for item in items:
                _check_value(item)
            ok = True
    if not ok:
        match = _ENUM_VALUE.fullmatch(text)
        if (
            match
            and _CLASS_SET.fullmatch(match.group(1))
            and (match.group(2) == "*" or _is_name(match.group(2)))
        ):
            _check_value(match.group(3))
            ok = True
    if not ok:
        raise DumpError(f"malformed value {text!r}")


def _class_set(text: str) -> "set[str]":
    return set(text[1:-1].split(","))


def values_equal(a: str, b: str) -> bool:
    """Return True when encoded values *a* and *b* name the same value.

    Equal text is equal. Two enum values are equal when their class sets
    intersect and their member name and value are equal, so adding a public
    alias of the enum is not a change. Tuples and frozensets compare element-wise.
    """
    if a == b:
        return True
    ma, mb = _ENUM_VALUE.fullmatch(a), _ENUM_VALUE.fullmatch(b)
    if ma and mb:
        return (
            bool(_class_set(ma.group(1)) & _class_set(mb.group(1)))
            and ma.group(2) == mb.group(2)
            and values_equal(ma.group(3), mb.group(3))
        )
    if a[:2] == b[:2] and a[:2] in ("T:", "Z:"):
        left, right = json.loads(a[2:]), json.loads(b[2:])
        if len(left) != len(right):
            return False
        if a[:2] == "T:":
            return all(values_equal(x, y) for x, y in zip(left, right, strict=True))
        unmatched = list(right)
        for x in left:
            hit = next((y for y in unmatched if values_equal(x, y)), None)
            if hit is None:
                return False
            unmatched.remove(hit)
        return True
    return False


@dataclass
class Record:
    """One dump line: its kind, its key, and the fields after them."""

    kind: str
    key: str
    fields: list[str]


@dataclass
class Param:
    """One ``<kind>:<name>:<default>`` parameter token; *default* None when absent."""

    kind: str
    name: str
    default: "str | None"


@dataclass
class Call:
    """A parsed ``call`` record or method call: params, or a ``@builtin`` constructor."""

    callkind: str
    params: "list[Param] | None"
    builtin: "str | None"


def binding_of(rec: Record) -> str:
    """Return the binding key *rec* belongs to: a member's owner, else its own key."""
    return rec.key.rpartition(".")[0] if rec.kind == "member" else rec.key


def identity(rec: Record) -> tuple:
    """Return *rec*'s identity: kind and key, plus the sub-key for ``input`` and ``enum``."""
    if rec.kind in ("input", "enum"):
        return (rec.kind, rec.key, rec.fields[0])
    return (rec.kind, rec.key)


def split_list(field: str) -> "list[str]":
    """Split a list field into its elements; ``-`` is the empty list."""
    return [] if field == EMPTY else field.split(" ")


def _parse_params(field: str) -> "list[Param]":
    params = []
    for token in split_list(field):
        match = _PARAM.fullmatch(token)
        if match is None or not _is_name(match.group(2)):
            raise DumpError(f"malformed parameter {token!r}")
        kind, name, default = match.groups()
        if default != EMPTY:
            _check_value(default)
        params.append(Param(kind, name, None if default == EMPTY else default))
    return params


def render_params(params: "list[Param]") -> str:
    """Render *params* as one list field."""
    if not params:
        return EMPTY
    return " ".join(
        f"{p.kind}:{p.name}:{EMPTY if p.default is None else p.default}" for p in params
    )


def _check_call_fields(callkind: str, params: str) -> None:
    if callkind not in CALL_KINDS:
        raise DumpError(f"unknown call kind {callkind!r}")
    if not _BUILTIN.fullmatch(params):
        _parse_params(params)


_HEADER_LINES = 2
_MIN_PARTS = 3  # kind, key and at least one field
_METHOD_FIELDS = 3  # a method member: its form, call kind and parameters
_FORMAT_FIELDS = 2  # a format: its reads and its writes
_METHOD_FORMS = ("method", "classmethod", "staticmethod")
_ARITY = {"name": 1, "mro": 1, "call": 2, "input": 4, "abstract": 1, "requires": 1, "enum": 2}


def _check_member(fields: "list[str]", line: str) -> None:
    if fields[0] in _METHOD_FORMS:
        if len(fields) != _METHOD_FIELDS:
            raise DumpError(f"a {fields[0]} member needs a call: {line!r}")
        _check_call_fields(fields[1], fields[2])
    elif len(fields) != 1 or not (
        fields[0] in ("field", "attribute", "class")
        or _PROPERTY.fullmatch(fields[0])
        or (fields[0].startswith("classref=") and _is_key(fields[0][9:]))
    ):
        raise DumpError(f"malformed member {line!r}")


def _check_input(fields: "list[str]") -> None:
    _check_value(fields[0])
    if fields[1] not in ("required", "optional"):
        raise DumpError(f"malformed input requiredness {fields[1]!r}")
    if fields[2] != EMPTY:
        _check_value(fields[2])
    for route in split_list(fields[3]):
        _check_value(route)


def _check_fields(kind: str, fields: "list[str]") -> None:
    """Check the fields of a non-member record of *kind* (arity already checked)."""
    if kind == "name" and fields[0] not in BINDING_KINDS:
        raise DumpError(f"unknown binding kind {fields[0]!r}")
    if kind == "mro":
        for entry in split_list(fields[0]):
            if not (_BUILTIN.fullmatch(entry) or _CLASS_SET.fullmatch(entry)):
                raise DumpError(f"malformed mro entry {entry!r}")
    if kind == "call":
        _check_call_fields(fields[0], fields[1])
    if kind in ("abstract", "requires"):
        for name in split_list(fields[0]):
            if not _is_name(name):
                raise DumpError(f"malformed name {name!r}")
    if kind == "input":
        _check_input(fields)
    if kind == "enum":
        if not _is_name(fields[0]):
            raise DumpError(f"malformed enum member {fields[0]!r}")
        _check_value(fields[1])


def _parse_format(key: str, fields: "list[str]", line: str) -> Record:
    """Parse a ``format`` record's name and its two version lists (dump spec §13.2)."""
    if not _FORMAT_NAME.fullmatch(key) or len(fields) != _FORMAT_FIELDS:
        raise DumpError(f"malformed format record {line!r}")
    lists = [split_list(f) for f in fields]
    for versions in lists:
        for version in versions:
            if not _VERSION.fullmatch(version):
                raise DumpError(f"a format version is I: or S:, got {version!r}")
            _check_value(version)
        if versions != sorted(set(versions)):
            raise DumpError(f"format versions must be sorted and distinct: {line!r}")
    if not lists[0] and not lists[1]:
        raise DumpError(f"a format declares at least one version: {line!r}")
    return Record(FORMAT, key, fields)


def parse_record(line: str) -> Record:
    """Parse one record line; raise :class:`DumpError` on anything malformed."""
    parts = line.split("\t")
    if len(parts) < _MIN_PARTS:
        raise DumpError(f"malformed record {line!r}")
    kind, key, fields = parts[0], parts[1], parts[2:]
    if kind not in KIND_ORDER:
        raise DumpError(f"unknown kind {kind!r} in {line!r}")
    if kind == FORMAT:
        return _parse_format(key, fields, line)
    if not _is_key(key):
        raise DumpError(f"malformed key {key!r}")
    if kind == "member":
        _check_member(fields, line)
    else:
        if len(fields) != _ARITY[kind]:
            raise DumpError(f"{kind} takes {_ARITY[kind]} field(s): {line!r}")
        _check_fields(kind, fields)
    return Record(kind, key, fields)


def render_record(rec: Record) -> str:
    """Render *rec* as one line, without its newline."""
    return "\t".join([rec.kind, rec.key, *rec.fields])


def parse_call(rec: Record) -> Call:
    """Read the call a ``call`` record or a method ``member`` record carries."""
    if rec.kind == "call":
        callkind, params = rec.fields[0], rec.fields[1]
    elif rec.kind == "member" and rec.fields[0] in _METHOD_FORMS:
        callkind, params = rec.fields[1], rec.fields[2]
    else:
        raise DumpError(f"{rec.kind} {rec.key} carries no call")
    if _BUILTIN.fullmatch(params):
        return Call(callkind, None, params.removeprefix("@builtin:"))
    return Call(callkind, _parse_params(params), None)


def _sort_key(rec: Record) -> tuple:
    if rec.kind == FORMAT:
        return (1, rec.key, 0, "")
    if rec.kind == "member":
        sub = rec.key.rpartition(".")[2]
    elif rec.kind == "input":
        sub = rec.fields[0]
    else:
        sub = ""  # enum keeps the producer's definition order (a stable sort)
    return (0, binding_of(rec), KIND_ORDER.index(rec.kind), sub)


def render_dump(records: "list[Record]", schema: int = PRODUCER_SCHEMA) -> str:
    """Render a whole dump: the two header lines, then *records* in canonical order."""
    lines = [render_record(r) for r in sorted(records, key=_sort_key)]
    return f"{V2_HEADER}\n{SCHEMA_PREFIX}{schema}\n" + "".join(f"{line}\n" for line in lines)


def producer_schema_of(text: str) -> "int | None":
    """Return the ``# producer-schema`` number on *text*'s second line, else None."""
    lines = text.split("\n", 2)[:2]
    if len(lines) == _HEADER_LINES and lines[1].startswith(SCHEMA_PREFIX):
        number = lines[1][len(SCHEMA_PREFIX) :]
        if number.isascii() and number.isdigit():
            return int(number)
    return None


class Dump:
    """A parsed dump, indexed by binding."""

    def __init__(self, schema: int, records: "list[Record]") -> None:
        self.schema = schema
        self.records: dict[tuple, Record] = {}
        self.bindings: dict[str, str] = {}
        self.formats: dict[str, Record] = {}
        self._members: dict[str, dict[str, Record]] = {}
        self._inputs: dict[str, dict[str, Record]] = {}
        self._enums: dict[str, dict[str, Record]] = {}
        for rec in records:
            ident = identity(rec)
            if ident in self.records:
                raise DumpError(f"duplicate record {ident!r}")
            self.records[ident] = rec
            if rec.kind == "name":
                self.bindings[rec.key] = rec.fields[0]
            elif rec.kind == FORMAT:
                self.formats[rec.key] = rec
        for rec in records:
            if rec.kind == FORMAT:
                continue
            owner = binding_of(rec)
            kind = self.bindings.get(owner)
            if kind is None:
                raise DumpError(f"{rec.kind} record for {rec.key} has no name record for {owner}")
            if rec.kind not in ALLOWED[kind]:
                raise DumpError(f"{rec.kind} record is not allowed on a {kind} binding: {rec.key}")
            index = {"member": self._members, "input": self._inputs, "enum": self._enums}.get(
                rec.kind
            )
            if index is not None:
                sub = rec.key.rpartition(".")[2] if rec.kind == "member" else rec.fields[0]
                index.setdefault(owner, {})[sub] = rec
        for key, kind in self.bindings.items():
            for needed in sorted(REQUIRED.get(kind, set())):
                if (needed, key) not in self.records:
                    raise DumpError(f"{kind} binding {key} is missing {needed}")

    def get(self, kind: str, key: str) -> "Record | None":
        """Return the unique *kind* record of *key*, or None."""
        return self.records.get((kind, key))

    def members_of(self, key: str) -> "dict[str, Record]":
        """Return *key*'s member records, by member name."""
        return self._members.get(key, {})

    def inputs_of(self, key: str) -> "dict[str, Record]":
        """Return *key*'s input records, by encoded input name."""
        return self._inputs.get(key, {})

    def enums_of(self, key: str) -> "dict[str, Record]":
        """Return *key*'s enum records, by member name, in definition order."""
        return self._enums.get(key, {})


def parse_dump(text: str) -> Dump:
    """Parse a whole dump; raise :class:`DumpError` on any structural fault."""
    if not text.endswith("\n"):
        raise DumpError("a dump ends with a newline")
    lines = text[:-1].split("\n")
    if not lines or lines[0] != V2_HEADER:
        raise DumpError(f"line 1 must be the header {V2_HEADER!r}")
    schema = producer_schema_of(text)
    if schema is None:
        raise DumpError(f"line 2 must be {SCHEMA_PREFIX!r}<n>")
    return Dump(schema, [parse_record(line) for line in lines[2:]])
