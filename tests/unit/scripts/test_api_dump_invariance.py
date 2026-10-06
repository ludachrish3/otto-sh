"""The dump is byte-identical on every interpreter and under any hash seed (dump spec §6, D-3).

No ``interpreter_agnostic`` marker: this module must run on every interpreter of
the nox matrix, because what it pins is that the minors agree.
"""

import sys
from pathlib import Path

import pytest

from scripts import api_records, api_regen
from tests._fixtures.api_dump import write_tree
from tests._fixtures.api_dump_surface import DRIFT_FILES, FILES, NAMESPACES, drift_expected
from tests._fixtures.paths import PROJECT_ROOT

EXPECTED = PROJECT_ROOT / "tests" / "_fixtures" / "api_dump_surface_expected.txt"


def render(
    seed: str, root: Path, files: "dict[str, str]" = FILES, namespaces: "list[str]" = NAMESPACES
) -> str:
    """Return the dump of fixture *files* (default: the surface) under hash seed *seed*."""
    src = write_tree(root, files)
    report = api_regen.run_child(Path(sys.executable), src, namespaces, seed=seed)
    assert report["refusals"] == [], report["refusals"]
    assert report["provenance"] == [], report["provenance"]
    return api_records.render_dump([api_records.parse_record(r) for r in report["records"]])


@pytest.mark.parametrize("offset", [0, 1000])
def test_fixture_surface_dump_is_byte_identical_to_the_committed_one(tmp_path, offset):
    # A different seed on every minor, and two per minor: the committed text is
    # the one answer every interpreter and every seed must give.
    seed = str(sys.version_info.minor + offset)
    assert render(seed, tmp_path) == EXPECTED.read_text(encoding="utf-8")


def test_a_divergent_auto_enum_cannot_be_byte_identical_on_every_minor(tmp_path):
    # Section 6: the lane fails on an enum whose records differ across minors.
    # Each minor of the matrix pins its own answer, and the answers differ, so
    # no committed dump can be byte-identical to all of them.
    minor = sys.version_info.minor
    assert render(str(minor), tmp_path, DRIFT_FILES, ["otto"]) == drift_expected(minor)
    assert drift_expected(10) != drift_expected(11)


def test_the_fixture_exercises_what_section_6_names():
    # A fixture that silently lost a shape would keep passing; pin its coverage.
    dump = api_records.parse_dump(EXPECTED.read_text(encoding="utf-8"))
    kinds = dict(dump.bindings)
    assert kinds["otto:Perm"] == "enum"
    assert kinds["otto:Level"] == "enum"
    assert kinds["otto:Opts"] == "typeddict"
    assert kinds["otto:Req"] == "typeddict"
    assert kinds["otto:Model"] == "model"
    assert kinds["otto:Point"] == "class"
    assert kinds["otto:Outer.Inner"] == "class"
    # §3.1: "alias" is a typing alias only (`typing.get_origin(x) is not None`);
    # a second name for a class is classified as the class it names.
    assert kinds["otto.sub:P"] == "class"
    assert kinds["otto:PointAlias"] == "class"
    assert kinds["otto.sub:compute"] == "function"
    assert dump.get("call", "otto:Boom").fields[1].startswith("@builtin:")
    # §3.1 rule 2: a typing alias is an alias, however it is spelled.
    assert kinds["otto:IntList"] == "alias"
    assert kinds["otto:MaybeInt"] == "alias"
    assert kinds["otto:IntOrNone"] == "alias"
    assert kinds["otto:bound"] == "callable"
    assert kinds["otto:Text"] == "class"
    assert kinds["otto:Proto"] == "protocol"
    assert dump.get("abstract", "otto:Abs").fields == ["alpha mid zeta"]
    assert dump.get("requires", "otto:Proto").fields == ["first second"]
    assert dump.members_of("otto:Text") == {}
    # auto() values every minor computes alike, beside explicit ones
    assert [r.fields[1] for r in dump.enums_of("otto:Step").values()] == [
        "I:1",
        "I:2",
        "I:10",
        "I:11",
    ]
    assert [r.fields[1] for r in dump.enums_of("otto:Bits").values()] == [
        "I:1",
        "I:2",
        "I:16",
        "I:32",
    ]
    text = EXPECTED.read_text(encoding="utf-8")
    for needle in [
        ":*=I:",  # a composite flag default
        "Z:[",  # a frozenset
        "\\u0020",  # an escaped space
        "{otto.sub:Perm,otto:Perm}",  # a multi-binding class set on a default
        "{otto.sub:P,otto:Point,otto:PointAlias}",  # ... and on an mro
        "@builtin:builtins.ValueError",
        "E:{otto:Level}:*=I:5",  # a non-member IntFlag composite
    ]:
        assert needle in text, needle
