"""``scripts/api_records.py``: the API dump's grammar (dump spec §2)."""

import enum
import math

import pytest

from scripts import api_records

pytestmark = pytest.mark.interpreter_agnostic


def _no_enums(cls):
    return None


class Color(enum.Enum):
    RED = 1
    CRIMSON = 1  # noqa: PIE796 - an alias, on purpose


class Perm(enum.Flag):
    R = 4
    W = 2


def _enum_sets(cls):
    return {Color: "{otto:Color}", Perm: "{otto.a:Perm,otto:Perm}"}.get(cls)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, "N"),
        (True, "B:true"),
        (False, "B:false"),
        (0, "I:0"),
        (-12, "I:-12"),
        (1.5, "F:0x1.8000000000000p+0"),
        (float("inf"), "F:inf"),
        (float("-inf"), "F:-inf"),
        (b"\x00\xff", "Y:00ff"),
        ("plain", 'S:"plain"'),
        ((1, "a"), 'T:["I:1","S:\\"a\\""]'),
        (frozenset({"b", "a"}), 'Z:["S:\\"a\\"","S:\\"b\\""]'),
        (object(), "O"),
    ],
)
def test_encode_value(value, text):
    assert api_records.encode_value(value, _no_enums) == text


def _hook(*args, **kwargs):
    raise AssertionError("a value's own hook ran while encoding")


_HOOKS = (
    "__bool__",
    "__int__",
    "__index__",
    "__float__",
    "__str__",
    "__repr__",
    "__bytes__",
    "__iter__",
    "__eq__",
    "__hash__",
    "__gt__",
)


def _claiming(claimed):
    """Return an object whose ``__class__`` claims *claimed* and whose every hook raises."""
    namespace = dict.fromkeys(_HOOKS, _hook)
    namespace["__class__"] = property(lambda self: claimed)
    return type("Liar", (), namespace)()


@pytest.mark.parametrize(
    "claimed", [bool, int, float, str, bytes, tuple, frozenset, Color], ids=lambda t: t.__name__
)
def test_a_value_claiming_a_tracked_type_is_opaque(claimed):
    liar = _claiming(claimed)
    assert isinstance(liar, claimed)  # the spoof isinstance would have trusted
    assert api_records.encode_value(liar, _enum_sets) == "O"


def _hostile(base, value):
    """Return an instance of a *base* subclass whose every hook raises."""
    return type(f"Hostile{base.__name__}", (base,), dict.fromkeys(_HOOKS, _hook))(value)


@pytest.mark.parametrize(
    ("base", "value", "text"),
    [
        (int, 5, "I:5"),
        (float, 1.5, "F:0x1.8000000000000p+0"),
        (float, -math.inf, "F:-inf"),
        (str, "a b", 'S:"a\\u0020b"'),
        (bytes, b"\x01", "Y:01"),
        (tuple, (1, "a"), 'T:["I:1","S:\\"a\\""]'),
        (frozenset, {"b", "a"}, 'Z:["S:\\"a\\"","S:\\"b\\""]'),
    ],
    ids=["int", "float", "float-inf", "str", "bytes", "tuple", "frozenset"],
)
def test_a_real_subclass_is_encoded_by_its_base_without_running_its_hooks(base, value, text):
    assert api_records.encode_value(_hostile(base, value), _no_enums) == text


def test_nan_encodes_as_nan():
    assert api_records.encode_value(math.nan, _no_enums) == "F:nan"


def test_string_with_space_tab_and_unicode_round_trips():
    text = api_records.encode_value("a b\tc é", _no_enums)
    assert " " not in text
    assert "\t" not in text
    assert text == 'S:"a\\u0020b\\tc\\u0020\\u00e9"'
    rec = api_records.Record("member", "otto:C.m", ["method", "sync", f"PK:x:{text}"])
    line = api_records.render_record(rec)
    assert api_records.render_record(api_records.parse_record(line)) == line


def test_enum_member_encodes_class_set_name_and_value():
    assert api_records.encode_value(Color.RED, _enum_sets) == "E:{otto:Color}:RED=I:1"
    # the alias resolves to the canonical member
    assert api_records.encode_value(Color.CRIMSON, _enum_sets) == "E:{otto:Color}:RED=I:1"


def test_composite_flag_writes_star_not_the_runtime_name():
    assert (
        api_records.encode_value(Perm.R | Perm.W, _enum_sets) == "E:{otto.a:Perm,otto:Perm}:*=I:6"
    )


def test_enum_without_a_public_binding_is_refused():
    with pytest.raises(api_records.UndeclaredEnumError) as info:
        api_records.encode_value(Color.RED, _no_enums)
    assert info.value.cls is Color


def test_values_equal_matches_enum_class_sets_by_intersection():
    assert api_records.values_equal("E:{otto:Color}:RED=I:1", "E:{otto.x:Color,otto:Color}:RED=I:1")
    assert not api_records.values_equal("E:{otto:Color}:RED=I:1", "E:{otto:Color}:RED=I:2")
    assert not api_records.values_equal("E:{otto:Color}:RED=I:1", "E:{otto:Other}:RED=I:1")
    assert api_records.values_equal(
        'T:["E:{otto:Color}:RED=I:1"]', 'T:["E:{otto:Color,otto.b:Color}:RED=I:1"]'
    )
    assert not api_records.values_equal("I:1", "I:2")


GOOD = (
    "# api-snapshot v2\n"
    "# producer-schema 1\n"
    "name\totto:C\tclass\n"
    "mro\totto:C\t@builtin:builtins.ValueError\n"
    "call\totto:C\tsync\tPK:x:-\n"
    "member\totto:C.go\tmethod\tcoroutine\tKO:fast:B:false\n"
    "member\totto:C.size\tproperty:g--\n"
    "abstract\totto:C\t-\n"
    "name\totto:f\tfunction\n"
    "call\totto:f\tsync\t-\n"
)


def test_parse_then_render_is_identity():
    dump = api_records.parse_dump(GOOD)
    assert dump.schema == 1
    assert dump.bindings == {"otto:C": "class", "otto:f": "function"}
    assert sorted(dump.members_of("otto:C")) == ["go", "size"]
    assert api_records.render_dump(list(dump.records.values())) == GOOD


def test_render_dump_orders_bindings_then_kinds():
    dump = api_records.parse_dump(GOOD)
    shuffled = list(reversed(list(dump.records.values())))
    assert api_records.render_dump(shuffled) == GOOD


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (GOOD.replace("# api-snapshot v2\n", ""), "header"),
        (GOOD.replace("# producer-schema 1\n", ""), "producer-schema"),
        (GOOD + "bogus\totto:C\tx\n", "unknown kind"),
        (GOOD + "name\totto:C\tclass\n", "duplicate"),
        (GOOD + "member\totto:D.m\tattribute\n", "no name record"),
        (GOOD.replace("call\totto:f\tsync\t-\n", ""), "missing call"),
        (GOOD.replace("PK:x:-", "XX:x:-"), "parameter"),
        (GOOD.replace("B:false", "B:maybe"), "value"),
        (GOOD + "enum\totto:f\tA\tI:1\n", "not allowed"),
    ],
)
def test_parse_dump_refuses(text, message):
    with pytest.raises(api_records.DumpError, match=message):
        api_records.parse_dump(text)


def test_input_and_enum_records_repeat_by_sub_key():
    text = (
        "# api-snapshot v2\n# producer-schema 1\n"
        "name\totto:E\tenum\nmro\totto:E\t-\n"
        "enum\totto:E\tA\tI:1\nenum\totto:E\tB\tI:2\n"
        "name\totto:M\tmodel\nmro\totto:M\t-\n"
        'input\totto:M\tS:"x-y"\toptional\tI:0\tS:"x-y" S:"xy"\n'
        "abstract\totto:M\t-\n"
    )
    dump = api_records.parse_dump(text)
    assert list(dump.enums_of("otto:E")) == ["A", "B"]
    assert list(dump.inputs_of("otto:M")) == ['S:"x-y"']
    assert api_records.render_dump(list(dump.records.values())) == text


def test_parse_call_reads_params_and_builtin():
    call = api_records.parse_call(
        api_records.parse_record("call\totto:C\tsync\tPO:a:- VP:args:- VK:kw:-")
    )
    assert [(p.kind, p.name, p.default) for p in call.params] == [
        ("PO", "a", None),
        ("VP", "args", None),
        ("VK", "kw", None),
    ]
    builtin = api_records.parse_call(
        api_records.parse_record("call\totto:E\tsync\t@builtin:builtins.str")
    )
    assert builtin.params is None
    assert builtin.builtin == "builtins.str"


def test_producer_schema_of():
    assert api_records.producer_schema_of(GOOD) == 1
    assert api_records.producer_schema_of("otto:x\n") is None


FMT = 'format\tlink-sentinel\tS:"v1" S:"v2" S:"v3"\tS:"v1" S:"v3"'


def test_a_format_record_round_trips_and_needs_no_binding():
    rec = api_records.parse_record(FMT)
    assert rec.kind == "format"
    assert rec.key == "link-sentinel"
    assert api_records.render_record(rec) == FMT
    dump = api_records.parse_dump(api_records.render_dump([rec]))
    assert dump.formats["link-sentinel"] == rec
    assert dump.bindings == {}


def test_format_records_sort_after_every_binding():
    recs = [
        api_records.parse_record("format\tb-fmt\tI:1\t-"),
        api_records.parse_record("format\ta-fmt\t-\tI:2"),
        api_records.parse_record("name\totto:zz\tvalue"),
    ]
    body = api_records.render_dump(recs).splitlines()[2:]
    assert [line.split("\t")[1] for line in body] == ["otto:zz", "a-fmt", "b-fmt"]


@pytest.mark.parametrize(
    "line",
    [
        "format\tLink\tI:1\t-",  # not kebab-case
        "format\tlink\t-\t-",  # both empty
        "format\tlink\tI:2 I:1\t-",  # not sorted by encoded text
        "format\tlink\tI:1 I:1\t-",  # duplicate
        "format\tlink\tB:true\t-",  # not an int or a str
        "format\tlink\tI:1",  # arity
        "format\tlink\tI:1\t-\n",  # a trailing newline is not part of a field
        'format\tlink\tI:1\tS:"a"\n',
        "format\tlink\n\tI:1\t-",
    ],
)
def test_malformed_format_records_are_refused(line):
    with pytest.raises(api_records.DumpError):
        api_records.parse_record(line)


@pytest.mark.parametrize(
    "line",
    [
        "abstract\totto:C\tfoo\n",
        "name\totto:C\tclass\n",
        "name\totto:C\n\tclass",
        "mro\totto:C\t@builtin:builtins.object\n",
        "call\totto:f\tsync\t@builtin:builtins.dict\n",
        "member\totto:C.x\tproperty:g--\n",
        "member\totto:C.x\tfield\n",
        'input\totto:C\tS:"a"\trequired\t-\tS:"a"\n',
        'input\totto:C\tS:"a"\trequired\tF:0x1.0p+0\n\tS:"a"',
    ],
)
def test_a_trailing_newline_in_a_field_is_refused(line):
    with pytest.raises(api_records.DumpError):
        api_records.parse_record(line)


@pytest.mark.parametrize("number", ["²", "٣", "x"])
def test_a_non_ascii_producer_schema_is_no_schema(number):
    text = f"{api_records.V2_HEADER}\n{api_records.SCHEMA_PREFIX}{number}\n"
    assert api_records.producer_schema_of(text) is None
    with pytest.raises(api_records.DumpError):
        api_records.parse_dump(text)


def test_parse_call_on_a_non_method_member_is_a_dump_error():
    with pytest.raises(api_records.DumpError):
        api_records.parse_call(api_records.parse_record("member\totto:C.x\tfield"))
    with pytest.raises(api_records.DumpError):
        api_records.parse_call(api_records.parse_record("name\totto:C\tclass"))


def test_a_float_exponent_is_ascii_digits_only():
    digit = chr(0x0663)
    with pytest.raises(api_records.DumpError):
        api_records.parse_record("input\totto:C\tF:0x1.0p+" + digit + "\trequired\t-\t-")
    with pytest.raises(api_records.DumpError):
        api_records.parse_record("format\tlink\tI:" + digit + "\t-")


@pytest.mark.parametrize("text", ["I:01", "I:-0", "I:+1", "I:00"])
def test_the_int_grammar_is_canonical(text):
    with pytest.raises(api_records.DumpError):
        api_records.parse_record("format\tlink\t" + text + "\t-")
    with pytest.raises(api_records.DumpError):
        api_records._check_value(text)


@pytest.mark.parametrize("value", [0, 7, -7, 10**20])
def test_the_encoder_emits_only_canonical_ints(value):
    encoded = api_records.encode_value(value, lambda cls: None)
    api_records._check_value(encoded)
    assert encoded == "I:" + str(value)


def test_a_negative_int_is_no_format_version():
    with pytest.raises(api_records.DumpError):
        api_records.parse_record("format\tlink\tI:-1\t-")
    api_records.parse_record("format\tlink\tI:0 I:1\t-")


@pytest.mark.parametrize("value", [math.nan, -math.inf, math.inf])
def test_a_non_finite_float_round_trips_through_the_parser(value):
    text = api_records.encode_value(value, _no_enums)
    member = api_records.Record("member", "otto:C.m", ["method", "sync", f"KO:x:{text}"])
    dump = api_records.parse_dump(
        api_records.render_dump(
            [
                api_records.Record("name", "otto:C", ["class"]),
                api_records.Record("mro", "otto:C", ["-"]),
                api_records.Record("call", "otto:C", ["sync", f"PK:x:{text}"]),
                api_records.Record("abstract", "otto:C", ["-"]),
                member,
            ]
        )
    )
    for rec in (dump.get("call", "otto:C"), dump.members_of("otto:C")["m"]):
        (param,) = api_records.parse_call(rec).params
        assert param.default == text
    assert api_records.render_record(dump.members_of("otto:C")["m"]) == (
        api_records.render_record(member)
    )


# Built with chr(): Unicode identifiers, one of them ending in a combining mark
# (Python's identifier rule accepts it; a regex \w does not).
E_ACUTE = chr(0xE9)
CAP_E_ACUTE = chr(0xC9)
X_COMBINING = "x" + chr(0x301)
NO_BREAK_SPACE = chr(0xA0)


@pytest.mark.parametrize("name", [E_ACUTE, CAP_E_ACUTE, X_COMBINING, "_" + E_ACUTE])
def test_every_python_identifier_is_a_grammar_name(name):
    call = api_records.parse_record(f"call\totto:f\tsync\tPK:{name}:I:0")
    assert api_records.parse_call(call).params == [api_records.Param("PK", name, "I:0")]
    api_records.parse_record(f"enum\totto:E\t{name}\tI:1")
    api_records.parse_record(f"abstract\totto:C\t{name}")
    api_records.parse_record(f"name\totto.{name}:{name}.{name}\tclass")
    api_records.parse_record(f"member\totto:C.{name}\tclassref=otto:{name}")
    api_records._check_value(f"E:{{otto:E}}:{name}=I:1")


@pytest.mark.parametrize(
    "line",
    [
        "call\totto:f\tsync\tPK:1a:I:0",  # a leading digit
        "call\totto:f\tsync\tPK:a-b:I:0",  # a separator
        "call\totto:f\tsync\tPK:a" + NO_BREAK_SPACE + "b:I:0",
        "enum\totto:E\tA-B\tI:1",
        "enum\totto:E\t1A\tI:1",
        "enum\totto:E\tA" + NO_BREAK_SPACE + "\tI:1",
        "abstract\totto:C\ta.b",
        "name\totto:1f\tclass",
        "name\totto:f-g\tclass",
        "name\totto.a-b:f\tclass",
        "name\tother:f\tclass",
        "name\totto:f:g\tclass",
        "member\totto:C.x\tclassref=otto:a-b",
        "call\totto:f\tsync\tPK:x:E:{otto:E}:A-B=I:1",
        "call\totto:f\tsync\tPK:x:E:{otto:E}:1A=I:1",
    ],
)
def test_a_name_python_would_not_accept_is_refused(line):
    with pytest.raises(api_records.DumpError):
        api_records.parse_record(line)
