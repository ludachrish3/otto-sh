"""scripts/facade_sweep.py: which imports are facade sites, and how --apply re-points them."""

from pathlib import Path

import pytest

from scripts import facade_sweep

pytestmark = pytest.mark.interpreter_agnostic


def _write(root: Path, files: "dict[str, str]") -> None:
    for name, text in files.items():
        path = root / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


HOP = '_LAZY_ATTRS = {"Far": "otto.hop.deep", "Near": "otto.hop.deep"}\n'

TOY = {
    "otto/__init__.py": (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from otto.hop import Far\n"
        '_LAZY_EXPORTS = {"Far": ("otto.hop", "Far")}\n'
    ),
    "otto/pkg/__init__.py": (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from .impl import Thing\n"
        '_LAZY_ATTRS = {"Thing": "otto.pkg.impl", "Near": "otto.hop"}\n'
        '_LAZY_EXPORTS = {"impl": "otto.pkg.impl", "Pair": ("otto.pkg.impl", "Twin")}\n'
    ),
    "otto/pkg/impl.py": "Thing = Twin = 1\n",
    "otto/hop/__init__.py": HOP,
    "otto/hop/deep.py": "Far = Near = 1\n",
    "otto/examples/__init__.py": "",
    "otto/examples/taught.py": "from otto.pkg import Thing\n",
    "otto/use/__init__.py": "",
    "otto/use/a.py": (
        "from typing import TYPE_CHECKING\n"
        "from otto.pkg import Thing\n"
        "from ..pkg import Thing as T, impl\n"
        "from otto.pkg import Pair\n"
        "from otto.pkg import Near\n"
        "from otto import Far\n"
        "if TYPE_CHECKING:\n"
        "    from ..pkg import Thing\n"
        "def f():\n"
        "    from otto.pkg.impl import Thing\n"
        "    from otto.pkg import Missing\n"
        "    from .. import pkg\n"
    ),
    "otto/pkg/inner.py": "from . import Thing\n",
}


def test_the_finder_sees_every_spelling_and_passes_the_rest(tmp_path: Path) -> None:
    _write(tmp_path, TOY)
    impl = "import Thing from otto.pkg.impl"
    assert [s.describe() for s in facade_sweep.find_sites(tmp_path / "src")] == [
        f"otto/pkg/inner.py:1 [<module>] from otto.pkg import Thing -> {impl}",
        f"otto/use/a.py:2 [<module>] from otto.pkg import Thing -> {impl}",
        f"otto/use/a.py:3 [<module>] from otto.pkg import Thing -> {impl}",
        "otto/use/a.py:4 [<module>] from otto.pkg import Pair -> import Twin from otto.pkg.impl",
        "otto/use/a.py:5 [<module>] from otto.pkg import Near -> import Near from otto.hop.deep",
        "otto/use/a.py:6 [<module>] from otto import Far -> import Far from otto.hop.deep",
        f"otto/use/a.py:8 [<module> TYPE_CHECKING] from otto.pkg import Thing -> {impl}",
    ]


def test_the_finder_sees_a_read_through_a_bound_facade_package(tmp_path: Path) -> None:
    text = (
        "import otto.pkg as pkg\n"
        "import otto.pkg\n"
        "from otto import pkg as p2\n"
        "from .. import pkg as p3\n"
        "pkg.Thing\n"
        "otto.pkg.Thing\n"
        "p2.Thing\n"
        "def f():\n"
        "    return p3.Thing\n"
        "pkg.impl\n"
        "pkg.Missing\n"
        "otto.pkg.impl.Thing\n"
        "other.Thing\n"
    )
    _write(
        tmp_path,
        {
            "otto/pkg/__init__.py": (
                '_LAZY_ATTRS = {"Thing": "otto.pkg.impl"}\n'
                '_LAZY_EXPORTS = {"impl": "otto.pkg.impl"}\n'
            ),
            "otto/pkg/impl.py": "Thing = 1\n",
            "otto/use/__init__.py": "",
            "otto/use/a.py": text,
        },
    )
    by_hand = "(re-point it by hand) -> import Thing from otto.pkg.impl"
    assert [s.describe() for s in facade_sweep.find_sites(tmp_path / "src")] == [
        f"otto/use/a.py:5 [<module>] pkg.Thing reads otto.pkg's Thing {by_hand}",
        f"otto/use/a.py:6 [<module>] otto.pkg.Thing reads otto.pkg's Thing {by_hand}",
        f"otto/use/a.py:7 [<module>] p2.Thing reads otto.pkg's Thing {by_hand}",
        f"otto/use/a.py:9 [f] p3.Thing reads otto.pkg's Thing {by_hand}",
    ]
    assert facade_sweep.apply(tmp_path) == []  # --apply rewrites only from statements
    assert (tmp_path / "src/otto/use/a.py").read_text() == text


@pytest.mark.parametrize(
    "init",
    [
        # nested: a TYPE_CHECKING import inside a function is not the table's mirror
        (
            "from typing import TYPE_CHECKING\n"
            "def f():\n"
            "    if TYPE_CHECKING:\n"
            "        from otto.hop import Far\n"
            '_LAZY_EXPORTS = {"Far": ("otto.hop", "Far")}\n'
        ),
        # aliased onto another table name: the binding does not match the entry
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from otto.hop import Near as Far\n"
            '_LAZY_EXPORTS = {"Far": ("otto.hop", "Far"), "Near": ("otto.hop", "Near")}\n'
        ),
        # an import the table does not name for that local name
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from otto.hop import Far\n"
            '_LAZY_EXPORTS = {"Far": ("otto.elsewhere", "Far")}\n'
        ),
    ],
)
def test_only_the_facades_own_mirror_is_exempt(tmp_path: Path, init: str) -> None:
    _write(
        tmp_path,
        {
            "otto/__init__.py": init,
            "otto/hop/__init__.py": HOP,
            "otto/hop/deep.py": "Far = Near = 1\n",
        },
    )
    assert [s.path for s in facade_sweep.find_sites(tmp_path / "src")] == ["otto/__init__.py"]


def test_apply_re_points_each_statement_and_keeps_its_spelling(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "otto/pkg/__init__.py": (
                '_LAZY_ATTRS = {"Thing": "otto.pkg.impl", "Other": "otto.pkg.more"}\n'
                '_LAZY_EXPORTS = {"impl": "otto.pkg.impl", "Pair": ("otto.pkg.impl", "Twin")}\n'
            ),
            "otto/use/__init__.py": "",
            "otto/use/a.py": (
                "from ..pkg import Thing, impl  # keep me\n"
                "from otto.pkg import Pair as P, Other\n"
                "def f():\n"
                "    from ..pkg import Thing as T\n"
            ),
        },
    )
    changed = facade_sweep.apply(tmp_path)
    assert changed == [tmp_path / "src/otto/use/a.py"]
    assert (tmp_path / "src/otto/use/a.py").read_text() == (
        "from ..pkg import impl  # keep me\n"
        "from ..pkg.impl import Thing\n"
        "from otto.pkg.impl import Twin as P\n"
        "from otto.pkg.more import Other\n"
        "def f():\n"
        "    from ..pkg.impl import Thing as T\n"
    )
    assert facade_sweep.find_sites(tmp_path / "src") == []
    assert facade_sweep.apply(tmp_path) == []


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("import os; from ..pkg import Thing\n", "shares its line with another statement"),
        ("from ..pkg import (\n    Thing,  # why\n)\n", "carries a comment inside the statement"),
    ],
)
def test_apply_refuses_a_statement_it_cannot_carry(tmp_path: Path, text: str, problem: str) -> None:
    _write(
        tmp_path,
        {
            "otto/pkg/__init__.py": '_LAZY_ATTRS = {"Thing": "otto.pkg.impl"}\n',
            "otto/use/__init__.py": "",
            "otto/use/a.py": text,
        },
    )
    with pytest.raises(SystemExit, match=problem):
        facade_sweep.apply(tmp_path)
    assert (tmp_path / "src/otto/use/a.py").read_text() == text


def test_apply_writes_nothing_when_any_file_is_refused(tmp_path: Path) -> None:
    good = "from ..pkg import Thing\n"
    bad = "import os; from ..pkg import Thing\n"
    _write(
        tmp_path,
        {
            "otto/pkg/__init__.py": '_LAZY_ATTRS = {"Thing": "otto.pkg.impl"}\n',
            "otto/use/__init__.py": "",
            "otto/use/a.py": good,
            "otto/use/z.py": bad,
            "otto/use/zz.py": bad,
        },
    )
    with pytest.raises(SystemExit) as refused:
        facade_sweep.apply(tmp_path)
    assert (tmp_path / "src/otto/use/a.py").read_text() == good
    assert (tmp_path / "src/otto/use/z.py").read_text() == bad
    assert str(refused.value).splitlines() == [
        "otto/use/z.py:1 shares its line with another statement",
        "otto/use/zz.py:1 shares its line with another statement",
    ]


def _one_facade(tmp_path: Path, text: str) -> Path:
    _write(
        tmp_path,
        {
            "otto/pkg/__init__.py": '_LAZY_ATTRS = {"Thing": "otto.pkg.impl"}\n',
            "otto/use/__init__.py": "",
            "otto/use/a.py": text,
        },
    )
    return tmp_path / "src/otto/use/a.py"


def test_a_non_ascii_alias_does_not_hide_a_statement_sharing_its_line(tmp_path: Path) -> None:
    """AST columns are UTF-8 byte offsets: a wide alias must not shift the line's tail."""
    text = "from ..pkg import Thing as 名名名;x=1\n"
    file = _one_facade(tmp_path, text)
    with pytest.raises(SystemExit, match="shares its line with another statement"):
        facade_sweep.apply(tmp_path)
    assert file.read_text(encoding="utf-8") == text


@pytest.mark.parametrize(
    ("text", "rewritten"),
    [
        ("from ..pkg import Thing as 名  # keep\n", "from ..pkg.impl import Thing as 名  # keep\n"),
        ("from ..pkg import Thing  # keep   \n", "from ..pkg.impl import Thing  # keep\n"),
        (
            "from ..pkg import (Thing as 名名)  # keep\n",
            "from ..pkg.impl import Thing as 名名  # keep\n",
        ),
    ],
)
def test_apply_keeps_a_trailing_comment(tmp_path: Path, text: str, rewritten: str) -> None:
    file = _one_facade(tmp_path, text)
    assert facade_sweep.apply(tmp_path) == [file]
    assert file.read_text(encoding="utf-8") == rewritten


def test_a_table_that_is_not_a_literal_names_its_file(tmp_path: Path) -> None:
    _write(tmp_path, {"otto/pkg/__init__.py": '_LAZY_ATTRS = {"Thing": MODULE}\n'})
    with pytest.raises(
        SystemExit, match=r"otto/pkg/__init__\.py: lazy table is not a literal dict"
    ):
        facade_sweep.lazy_bindings(tmp_path / "src")
