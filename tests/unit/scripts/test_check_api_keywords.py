"""``scripts/check_api_keywords.py``: positional parameters only where the manifest lists them.

Design ``docs/superpowers/specs/2026-11-07-keyword-only-public-interfaces-design.md`` §5.
The dumps are planted text: the rule reads a dump, it never produces one.
"""

import pytest

from scripts import api_records, api_regen, check_api_keywords
from scripts.api_manifest import parse_symbols

pytestmark = pytest.mark.interpreter_agnostic

_HEAD = f"{api_records.V2_HEADER}\n{api_records.SCHEMA_PREFIX}{api_records.PRODUCER_SCHEMA}\n"


_NAMESPACES = '[namespaces."otto.m"]\ntier = 1\nstability = "stable"\n'


def _dump(*lines: str) -> api_records.Dump:
    return api_records.parse_dump(_HEAD + "".join(f"{line}\n" for line in lines))


def _function(name: str, params: str) -> "list[str]":
    key = f"otto.m:{name}"
    return [f"name\t{key}\tfunction", f"call\t{key}\tsync\t{params}"]


def _class(name: str, mro: str = "-", members: "dict[str, str] | None" = None) -> "list[str]":
    key = f"otto.m:{name}"
    lines = [
        f"name\t{key}\tclass",
        f"mro\t{key}\t{mro}",
        f"call\t{key}\tsync\t-",
        f"abstract\t{key}\t-",
    ]
    return lines + [
        f"member\t{key}.{m}\tmethod\tsync\t{params}" for m, params in (members or {}).items()
    ]


def _protocol(name: str, members: "dict[str, str]") -> "list[str]":
    key = f"otto.m:{name}"
    return [
        f"name\t{key}\tprotocol",
        f"mro\t{key}\t-",
        f"requires\t{key}\t{' '.join(sorted(members))}",
        *(f"member\t{key}.{m}\tmethod\tsync\t{p}" for m, p in members.items()),
    ]


def _symbols(manifest: str) -> dict:
    return parse_symbols(_NAMESPACES + manifest)


def _findings(lines: "list[str]", manifest: str = "") -> "list[str]":
    return check_api_keywords.findings(_dump(*lines), _symbols(manifest))


def _unlisted(key: str, name: str) -> str:
    return (
        f"{key}: positional parameter {name} — make the record kw_only, "
        f'or list it under [symbols."{key}"].positional'
    )


def test_an_unlisted_positional_is_a_finding():
    assert _findings(_function("f", "PK:a:- KO:b:-")) == [_unlisted("otto.m:f", "a")]


def test_a_listed_prefix_passes():
    manifest = '[symbols."otto.m:f"]\npositional = ["a"]\n'
    assert _findings(_function("f", "PK:a:- KO:b:-"), manifest) == []


def test_equality_not_containment():
    manifest = '[symbols."otto.m:f"]\npositional = ["a"]\n'
    assert _findings(_function("f", "PK:a:- PK:b:-"), manifest) == [_unlisted("otto.m:f", "b")]


def test_a_listed_name_that_is_keyword_only_fails():
    manifest = '[symbols."otto.m:f"]\npositional = ["a", "b"]\nwhy = "w"\n'
    assert _findings(_function("f", "PK:a:- KO:b:-"), manifest) == [
        "otto.m:f: b is listed positional but keyword-only in the code"
    ]


def test_a_trailing_vp_is_covered_by_its_name():
    manifest = '[symbols."otto.m:f"]\npositional = ["a", "args"]\nwhy = "printf-like"\n'
    assert _findings(_function("f", "PK:a:- VP:args:-"), manifest) == []
    assert _findings(_function("f", "VP:args:-")) == [
        "otto.m:f: *args accepts positionals; list it with a why"
    ]


def test_a_protocol_entry_reaches_a_structural_implementer():
    lines = [
        *_protocol("Host", {"run": "PK:cmd:-"}),
        *_class("Unix", members={"run": "PK:cmd:-", "close": "-"}),
        *_class("Other", members={"close": "PK:x:-"}),
    ]
    manifest = '[symbols."otto.m:Host.run"]\npositional = ["cmd"]\n'
    assert _findings(lines, manifest) == [_unlisted("otto.m:Other.close", "x")]
    entries = check_api_keywords.effective_entries(_dump(*lines), _symbols(manifest))
    assert entries["otto.m:Unix.run"][1] == ["otto.m:Host.run"]
    assert "otto.m:Other.close" not in entries


def test_a_base_entry_reaches_a_subclass_via_mro():
    lines = [
        *_class("Base", members={"run": "PK:cmd:-"}),
        *_class("Sub", mro="{otto.m:Base}", members={"run": "PK:cmd:-"}),
    ]
    manifest = '[symbols."otto.m:Base.run"]\npositional = ["cmd"]\n'
    assert _findings(lines, manifest) == []
    entries = check_api_keywords.effective_entries(_dump(*lines), _symbols(manifest))
    assert entries["otto.m:Sub.run"][1] == ["otto.m:Base.run"]


def test_an_override_that_renames_fails():
    lines = [
        *_class("Base", members={"run": "PK:cmd:-"}),
        *_class("Sub", mro="{otto.m:Base}", members={"run": "PK:command:-"}),
    ]
    manifest = '[symbols."otto.m:Base.run"]\npositional = ["cmd"]\n'
    assert _findings(lines, manifest) == ["otto.m:Sub.run: override reorders or renames ['cmd']"]


def test_disagreeing_sources_fail():
    lines = [
        *_class("Base", members={"run": "PK:cmd:- PK:t:-"}),
        *_class("Sub", mro="{otto.m:Base}", members={"run": "PK:cmd:- PK:t:-"}),
    ]
    manifest = (
        '[symbols."otto.m:Base.run"]\npositional = ["cmd", "t"]\nwhy = "w"\n'
        '[symbols."otto.m:Sub.run"]\npositional = ["cmd"]\n'
    )
    assert _findings(lines, manifest) == [
        (
            "otto.m:Sub.run: entries disagree: otto.m:Sub.run says ['cmd'], "
            "otto.m:Base.run says ['cmd', 't']"
        ),
        _unlisted("otto.m:Sub.run", "t"),
    ]


def test_dunder_interpreter_slots_pass_and_extra_args_fail():
    lines = _class("C", members={"__eq__": "PK:other:-", "__call__": "PK:condition:- PK:msg:-"})
    assert _findings(lines) == [
        _unlisted("otto.m:C.__call__", "condition"),
        _unlisted("otto.m:C.__call__", "msg"),
    ]
    extra = _class("D", members={"__getitem__": "PK:key:- PK:extra:-"})
    assert _findings(extra) == [_unlisted("otto.m:D.__getitem__", "extra")]


def test_an_entry_listing_a_dunder_slot_passes():
    lines = _class("C", members={"__eq__": "PK:other:-"})
    assert _findings(lines, '[symbols."otto.m:C.__eq__"]\npositional = ["other"]\n') == []


def test_a_po_without_why_fails_even_when_inherited():
    lines = [
        *_class("Base", members={"f": "PK:x:-"}),
        *_class("Sub", mro="{otto.m:Base}", members={"f": "PO:x:-"}),
    ]
    manifest = '[symbols."otto.m:Base.f"]\npositional = ["x"]\n'
    assert _findings(lines, manifest) == ["otto.m:Sub.f: positional-only x needs a why"]
    variadic = _function("g", "VP:args:-")
    assert _findings(variadic, '[symbols."otto.m:g"]\npositional = ["args"]\n') == [
        "otto.m:g: variadic args needs a why"
    ]


def test_a_withdrawn_call_is_skipped():
    lines = [
        "name\totto.m:C\tclass",
        "mro\totto.m:C\t-",
        f"call\totto.m:C\tsync\t{api_records.WITHDRAWN_CONSTRUCT}",
        "abstract\totto.m:C\t-",
        *_function("err", "@builtin:builtins.Exception"),
    ]
    assert _findings(lines) == []


def test_main_exit_codes(monkeypatch, tmp_path, capsys):
    manifest = tmp_path / "public.toml"
    manifest.write_text("")
    clean = _HEAD + "".join(f"{line}\n" for line in _function("f", "KO:a:-"))
    dirty = _HEAD + "".join(f"{line}\n" for line in _function("f", "PK:a:-"))

    def generated(text, refusals=()):
        return lambda *a, **k: api_regen.Generated(text, list(refusals))

    monkeypatch.setattr(api_regen, "generate_worktree", generated(clean))
    assert check_api_keywords.main(["--manifest", str(manifest)]) == 0
    monkeypatch.setattr(api_regen, "generate_worktree", generated(dirty))
    assert check_api_keywords.main(["--manifest", str(manifest)]) == 1
    assert _unlisted("otto.m:f", "a") in capsys.readouterr().out
    monkeypatch.setattr(api_regen, "generate_worktree", generated(None, ["manifest: bad"]))
    assert check_api_keywords.main(["--manifest", str(manifest)]) == 1
    assert "manifest: bad" in capsys.readouterr().out


def test_dunder_entries_on_two_sources_agree_on_their_slots():
    lines = [
        *_class("Base", members={"__eq__": "PK:other:-"}),
        *_class("Sub", mro="{otto.m:Base}", members={"__eq__": "PK:other:-"}),
    ]
    manifest = (
        '[symbols."otto.m:Base.__eq__"]\npositional = ["other"]\n'
        '[symbols."otto.m:Sub.__eq__"]\npositional = ["other"]\n'
    )
    assert _findings(lines, manifest) == []


def test_an_override_that_only_adds_a_positional_names_it():
    lines = [
        *_class("Base", members={"run": "PK:cmds:-"}),
        *_class("Sub", mro="{otto.m:Base}", members={"run": "PK:cmds:- PK:timeout:-"}),
    ]
    manifest = '[symbols."otto.m:Base.run"]\npositional = ["cmds"]\n'
    assert _findings(lines, manifest) == [_unlisted("otto.m:Sub.run", "timeout")]
    shorter = [
        *_class("Base", members={"run": "PK:cmds:- PK:t:-"}),
        *_class("Sub", mro="{otto.m:Base}", members={"run": "PK:cmds:-"}),
    ]
    two = '[symbols."otto.m:Base.run"]\npositional = ["cmds", "t"]\nwhy = "w"\n'
    assert _findings(shorter, two) == ["otto.m:Sub.run: override reorders or renames ['cmds', 't']"]


def test_a_class_call_record_is_judged_by_its_own_entry():
    # A NamedTuple's call record: its fields are its positional constructor.
    lines = [
        "name\totto.m:T\tclass",
        "mro\totto.m:T\t@builtin:builtins.tuple",
        "call\totto.m:T\tsync\tPK:a:- PK:b:-",
        "abstract\totto.m:T\t-",
    ]
    assert _findings(lines) == [_unlisted("otto.m:T", "a"), _unlisted("otto.m:T", "b")]
    manifest = '[symbols."otto.m:T"]\npositional = ["a", "b"]\nwhy = "a tuple record"\n'
    assert _findings(lines, manifest) == []
    assert _findings(lines, '[symbols."otto.m:T"]\npositional = ["a"]\n') == [
        _unlisted("otto.m:T", "b")
    ]


def test_a_listed_po_with_a_why_passes():
    manifest = '[symbols."otto.m:f"]\npositional = ["x"]\nwhy = "mirrors the builtin"\n'
    assert _findings(_function("f", "PO:x:- KO:y:-"), manifest) == []
