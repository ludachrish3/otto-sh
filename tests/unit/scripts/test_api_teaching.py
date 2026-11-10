"""``scripts/api_teaching.py``: every otto name the docs and shipped examples teach is declared."""

import pytest

from scripts import api_teaching
from scripts.api_manifest import load_manifest
from scripts.api_teaching import StaticDeclaration, check_corpus, check_file, setup_otto_names
from scripts.api_teaching import main as teaching_main
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

DECL = StaticDeclaration(
    members={
        "otto": {"Status", "load_lab", "Result", "Results", "expect"},
        "otto.docker": {"deploy"},
        "otto.host": {"Element", "LocalHost", "host_identity", "expect"},
        "otto.suite": {"ExpectCollector"},
        "otto.examples": set(),
    },
    private={
        "otto:Result": set(),
        "otto:Results": set(),
        "otto.host:LocalHost": set(),
        "otto.host:Element": set(),
        "otto.suite:ExpectCollector": set(),
    },
    # The dump's positional shapes: a constructor, a function, a method, a classmethod,
    # a ``__call__``, a re-export that agrees with its home, and one whose two homes
    # disagree (``load_lab``). ``receivers`` names the plain methods.
    shapes={
        "otto:Result": 0,
        "otto:Results.collect": 0,
        "otto:expect": 1,
        "otto.host:expect": 1,
        "otto:load_lab": 1,
        "otto.host:LocalHost": 0,
        "otto.host:LocalHost.run": 1,
        "otto.host:Element": None,
        "otto.suite:ExpectCollector": 0,
        "otto.suite:ExpectCollector.__call__": 1,
    },
    receivers={"otto.host:LocalHost.run", "otto.suite:ExpectCollector.__call__"},
)


def _page(tmp_path, body, name="p.md"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _kinds(path, tmp_path, setup=frozenset(), corpus=None):
    findings = check_file(
        path, DECL, set(setup), repo_root=tmp_path, corpus=corpus if corpus is not None else {path}
    )
    return [(f.kind, f.detail, f.line) for f in findings]


def test_declared_imports_pass(tmp_path):
    page = _page(tmp_path, "```python\nfrom otto.host import Element\nimport otto.docker\n```\n")
    assert _kinds(page, tmp_path) == []


def test_import_of_an_undeclared_namespace_is_reported(tmp_path):
    page = _page(tmp_path, "```python\nimport otto.nope\n```\n")
    assert _kinds(page, tmp_path) == [("undeclared-namespace", "otto.nope", 2)]


def test_undeclared_from_import_is_reported_with_its_line(tmp_path):
    page = _page(tmp_path, "Intro.\n\n```python\nfrom otto.host.element import Element\n```\n")
    assert _kinds(page, tmp_path) == [("undeclared-import", "otto.host.element:Element", 4)]


def test_attribute_after_module_import_is_checked(tmp_path):
    page = _page(
        tmp_path,
        "```python\nimport otto.docker\notto.docker.deploy()\notto.docker.compose_up()\n```\n",
    )
    assert _kinds(page, tmp_path) == [("undeclared-attribute", "otto.docker:compose_up", 4)]


def test_bindings_carry_across_blocks_of_one_page(tmp_path):
    page = _page(tmp_path, "```python\nimport otto\n```\n\nText.\n\n```python\notto.nope()\n```\n")
    assert _kinds(page, tmp_path) == [("undeclared-attribute", "otto:nope", 8)]


def test_alias_is_tracked_and_rebinding_ends_it(tmp_path):
    page = _page(
        tmp_path,
        "```python\nimport otto.docker as d\nd.nope()\nd = object()\nd.anything\n```\n",
    )
    assert _kinds(page, tmp_path) == [("undeclared-attribute", "otto.docker:nope", 3)]


def test_object_path_strings_are_checked_in_any_block(tmp_path):
    page = _page(
        tmp_path,
        '```python\nregister("link", "otto.cli.link:link_app")\n```\n'
        '```toml\nclass = "otto.host:Element"\n```\n',
    )
    assert _kinds(page, tmp_path) == [("undeclared-object-path", "otto.cli.link:link_app", 2)]


def test_star_import_is_reported(tmp_path):
    page = _page(tmp_path, "```python\nfrom otto import *\n```\n")
    assert [k for k, _, _ in _kinds(page, tmp_path)] == ["star-import"]


def test_setup_only_name_needs_a_visible_import(tmp_path):
    bare = _page(tmp_path, "```{doctest}\n>>> host = LocalHost()\n```\n", "a.md")
    assert _kinds(bare, tmp_path, {"LocalHost"}) == [("setup-only-name", "LocalHost", 2)]
    shown = _page(
        tmp_path,
        "```{doctest}\n>>> from otto.host import LocalHost\n>>> host = LocalHost()\n```\n",
        "b.md",
    )
    assert _kinds(shown, tmp_path, {"LocalHost"}) == []


def test_relative_import_in_a_shipped_example_is_resolved_and_checked(tmp_path):
    example = tmp_path / "src" / "otto" / "examples" / "demo.py"
    example.parent.mkdir(parents=True)
    example.write_text("from ..host.factory import host_identity\n", encoding="utf-8")
    assert _kinds(example, tmp_path) == [
        ("undeclared-import", "otto.host.factory:host_identity", 1)
    ]


def test_example_docstring_literal_block_is_checked(tmp_path):
    example = tmp_path / "src" / "otto" / "examples" / "demo.py"
    example.parent.mkdir(parents=True)
    example.write_text(
        '"""Demo.\n\nRegister it::\n\n    from otto.host.factory import x\n"""\n', encoding="utf-8"
    )
    assert _kinds(example, tmp_path) == [("undeclared-import", "otto.host.factory:x", 5)]


def test_a_docstring_doctest_does_not_turn_the_whole_file_into_a_doctest(tmp_path):
    example = tmp_path / "src" / "otto" / "examples" / "demo.py"
    example.parent.mkdir(parents=True)
    example.write_text(
        '"""Demo.\n\n>>> from otto.nope import A\n"""\n\nfrom otto.host.factory import x\n',
        encoding="utf-8",
    )
    assert _kinds(example, tmp_path) == [
        ("undeclared-import", "otto.host.factory:x", 6),
        ("undeclared-import", "otto.nope:A", 3),
    ]


def test_unparseable_block_still_checks_its_imports(tmp_path):
    page = _page(
        tmp_path,
        "```python\nfrom otto.host import Element\nfrom otto.nope import X\n"
        "host = <your host>\n```\n",
    )
    assert _kinds(page, tmp_path) == [("undeclared-import", "otto.nope:X", 3)]


def test_unparseable_block_still_checks_its_object_path_strings(tmp_path):
    example = tmp_path / "src" / "otto" / "examples" / "demo.py"
    example.parent.mkdir(parents=True)
    example.write_text(
        '"""Demo.\n\nIn the lab file::\n\n    [hosts.box]\n    class = "otto.nope:X"\n"""\n',
        encoding="utf-8",
    )
    assert _kinds(example, tmp_path) == [("undeclared-object-path", "otto.nope:X", 6)]


def test_an_import_that_will_not_parse_alone_is_reported(tmp_path):
    page = _page(tmp_path, "Intro.\n\n```python\nfrom otto.host import (Element,\nx = <y>\n```\n")
    assert [(k, line) for k, _, line in _kinds(page, tmp_path)] == [("unparseable-import", 4)]


def test_dynamic_import_of_otto_is_reported(tmp_path):
    page = _page(tmp_path, '```python\nimport importlib\nimportlib.import_module("otto.x")\n```\n')
    assert [k for k, _, _ in _kinds(page, tmp_path)] == ["dynamic-import"]


def _repo(tmp_path, files):
    """Lay out a tiny repo (a README is always part of the corpus) and return its root."""
    for rel, body in {"README.md": "Readme.\n", **files}.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def _corpus(root, setup=frozenset()):
    return [(f.path, f.line, f.kind, f.detail) for f in check_corpus(root, DECL, set(setup))]


def test_a_python_include_outside_the_corpus_is_checked_as_its_own_file(tmp_path):
    root = _repo(
        tmp_path,
        {
            "elsewhere.py": "import os\nfrom otto.nope import X\n",
            "docs/page.md": "```{literalinclude} ../elsewhere.py\n```\n",
        },
    )
    assert _corpus(root) == [("elsewhere.py", 2, "undeclared-import", "otto.nope:X")]


def test_a_non_python_include_has_its_object_path_strings_checked(tmp_path):
    root = _repo(
        tmp_path,
        {
            "lab.toml": '[hosts.box]\nclass = "otto.internal:X"\nok = "otto.host:Element"\n',
            "docs/page.md": "```{literalinclude} ../lab.toml\n```\n",
        },
    )
    assert _corpus(root) == [("lab.toml", 2, "undeclared-object-path", "otto.internal:X")]


def test_an_include_is_walked_once_however_many_pages_include_it(tmp_path):
    root = _repo(
        tmp_path,
        {
            "docs/examples/ex.py": "from otto.nope import X\n",
            "docs/a.md": "```{literalinclude} examples/ex.py\n```\n",
            "docs/b.md": "```{literalinclude} /examples/ex.py\n```\n",
        },
    )
    assert _corpus(root) == [("docs/examples/ex.py", 1, "undeclared-import", "otto.nope:X")]


def test_an_include_lends_the_page_no_bindings(tmp_path):
    root = _repo(
        tmp_path,
        {
            "shown.py": "from otto.host import LocalHost\n",
            "docs/page.md": "```{literalinclude} ../shown.py\n```\n\n```python\nLocalHost()\n```\n",
        },
    )
    assert _corpus(root, {"LocalHost"}) == [("docs/page.md", 5, "setup-only-name", "LocalHost")]


def test_a_missing_include_is_reported_on_the_page(tmp_path):
    root = _repo(tmp_path, {"docs/page.md": "Intro.\n\n```{literalinclude} gone.py\n```\n"})
    assert _corpus(root) == [("docs/page.md", 3, "include-missing", "docs/gone.py")]


def test_non_python_fences_are_not_parsed(tmp_path):
    page = _page(tmp_path, "```bash\nfrom otto.nope import X\n```\n")
    assert _kinds(page, tmp_path) == []


def test_setup_otto_names_reads_conf_py(tmp_path):
    conf = tmp_path / "conf.py"
    conf.write_text(
        'doctest_global_setup = f"""\nimport asyncio\nfrom otto import Status\n'
        'from otto.host.local_host import LocalHost as LH\nGS = {1!r}\n"""\n',
        encoding="utf-8",
    )
    assert setup_otto_names(conf) == {"Status", "LH"}


def test_code_before_the_first_prompt_of_a_python_fence_is_code(tmp_path):
    # A def whose docstring holds a doctest is not a REPL transcript: the lines before
    # the first ``>>> `` are code, so the import above it is checked, not dropped.
    page = _page(
        tmp_path,
        '```python\nfrom otto.nope import X\ndef f():\n    """\n    >>> f()\n    1\n    """\n```\n',
    )
    assert _kinds(page, tmp_path) == [("undeclared-import", "otto.nope:X", 2)]


def test_a_transcript_fence_still_drops_its_output_lines(tmp_path):
    page = _page(tmp_path, "```python\n>>> import os\nfrom otto.nope import X\n```\n")
    assert _kinds(page, tmp_path) == []


def test_setup_otto_names_reads_a_conf_py_with_an_invalid_escape(tmp_path):
    # pytest runs with filterwarnings=error: the escape's warning must not become a SyntaxError.
    conf = tmp_path / "conf.py"
    conf.write_text('X = "\\d"\ndoctest_global_setup = "from otto import Status"\n')
    assert setup_otto_names(conf) == {"Status"}


def test_object_path_string_names_a_declared_member_not_a_namespace(tmp_path):
    page = _page(
        tmp_path,
        '```python\nregister("otto:docker")\n```\n```toml\nclass = "otto:docker"\n```\n',
    )
    assert _kinds(page, tmp_path) == [
        ("undeclared-object-path", "otto:docker", 2),
        ("undeclared-object-path", "otto:docker", 5),
    ]


def test_from_import_of_a_declared_namespace_binds_that_namespace(tmp_path):
    page = _page(
        tmp_path, "```python\nfrom otto import docker\ndocker.deploy()\ndocker.nope()\n```\n"
    )
    assert _kinds(page, tmp_path) == [("undeclared-attribute", "otto.docker:nope", 4)]


def test_a_function_parameter_does_not_unbind_a_page_alias(tmp_path):
    page = _page(
        tmp_path,
        "```python\nimport otto.docker as d\ndef f(d):\n    return d.anything\nd.nope()\n```\n",
    )
    assert _kinds(page, tmp_path) == [("undeclared-attribute", "otto.docker:nope", 5)]


def test_lambda_comprehension_and_class_bindings_stay_local(tmp_path):
    page = _page(
        tmp_path,
        "```python\nimport otto.docker as d\nxs = [d for d in range(3)]\nf = lambda d: d\n"
        "class C:\n    d = 1\nd.nope()\n```\n",
    )
    assert _kinds(page, tmp_path) == [("undeclared-attribute", "otto.docker:nope", 7)]


def test_an_import_inside_a_function_is_checked_but_does_not_bind_the_page(tmp_path):
    page = _page(
        tmp_path,
        "```python\ndef f():\n    from otto.host import LocalHost\n    from otto.nope import X\n"
        "    return LocalHost()\nLocalHost()\n```\n",
    )
    assert _kinds(page, tmp_path, {"LocalHost"}) == [
        ("undeclared-import", "otto.nope:X", 4),
        ("setup-only-name", "LocalHost", 6),
    ]


def test_testsetup_bindings_carry_into_a_later_doctest(tmp_path):
    page = _page(
        tmp_path,
        "```{testsetup}\nfrom otto.host import LocalHost\nimport otto.docker as d\n```\n\n"
        "```{doctest}\n>>> LocalHost()\n>>> d.nope()\n```\n",
    )
    assert _kinds(page, tmp_path, {"LocalHost"}) == [
        ("undeclared-attribute", "otto.docker:nope", 8)
    ]


def test_fallback_checks_an_import_whatever_module_it_names_first(tmp_path):
    page = _page(tmp_path, "```python\nimport os, otto.internal\nx = <placeholder>\n```\n")
    assert _kinds(page, tmp_path) == [("undeclared-namespace", "otto.internal", 2)]


def test_fallback_checks_setup_only_names_on_lines_that_parse_and_lines_that_do_not(tmp_path):
    page = _page(
        tmp_path,
        "```python\nhost = LocalHost()\nother = LocalHost(<name>)\n```\n",
    )
    assert _kinds(page, tmp_path, {"LocalHost"}) == [
        ("setup-only-name", "LocalHost", 2),
        ("setup-only-name", "LocalHost", 3),
    ]


def test_fallback_binds_what_its_parsed_statements_import(tmp_path):
    page = _page(
        tmp_path,
        "```python\nfrom otto.host import LocalHost\nimport otto.docker as d\n"
        "h = LocalHost(<name>)\nd.nope(<x>)\n```\n",
    )
    assert _kinds(page, tmp_path, {"LocalHost"}) == [
        ("undeclared-attribute", "otto.docker:nope", 5)
    ]


def test_fallback_reports_an_object_path_once(tmp_path):
    page = _page(tmp_path, '```python\nx = "otto.nope:X"\ny = <p>\n```\n')
    assert _kinds(page, tmp_path) == [("undeclared-object-path", "otto.nope:X", 2)]


def test_a_non_identifier_after_an_alias_is_not_an_attribute(tmp_path):
    page = _page(tmp_path, "```python\nimport otto.docker as h\nh.264\nh.nope\n```\n")
    assert _kinds(page, tmp_path) == [("undeclared-attribute", "otto.docker:nope", 4)]


def test_a_docstring_directive_block_in_a_py_file_is_checked_like_a_page_block(tmp_path):
    example = tmp_path / "src" / "otto" / "examples" / "demo.py"
    example.parent.mkdir(parents=True)
    example.write_text(
        '"""Demo.\n\n.. code-block:: python\n\n    from otto.nope import X\n\n'
        ".. doctest::\n\n    >>> from otto.nope import Y\n\n"
        '.. testsetup::\n\n    from otto.nope import Z\n"""\n',
        encoding="utf-8",
    )
    assert sorted(_kinds(example, tmp_path), key=lambda k: k[2]) == [
        ("undeclared-import", "otto.nope:X", 5),
        ("undeclared-import", "otto.nope:Y", 9),
        ("undeclared-import", "otto.nope:Z", 13),
    ]


def test_a_python_file_that_will_not_parse_is_a_finding_not_an_abort(tmp_path):
    root = _repo(
        tmp_path,
        {
            "docs/examples/bad.py": "from otto.nope import A\ndef (:\n",
            "docs/page.md": "```python\nfrom otto.nope import B\n```\n",
        },
    )
    # The parser's message varies by Python version; the finding's place and kind do not.
    found = [(p, n, k, "<msg>" if k == "unparseable" else d) for p, n, k, d in _corpus(root)]
    assert found == [
        ("docs/examples/bad.py", 2, "unparseable", "<msg>"),
        ("docs/examples/bad.py", 1, "undeclared-import", "otto.nope:A"),
        ("docs/page.md", 2, "undeclared-import", "otto.nope:B"),
    ]


def test_an_unexpected_error_in_one_file_does_not_stop_the_corpus(tmp_path, monkeypatch):
    root = _repo(
        tmp_path,
        {
            "docs/a.md": "```python\nimport os\n```\n",
            "docs/b.md": "```python\nfrom otto.nope import B\n```\n",
        },
    )
    real = api_teaching.blocks.markdown_blocks

    def flaky(path):
        if path.name == "a.md":
            raise RuntimeError("boom")
        return real(path)

    monkeypatch.setattr(api_teaching.blocks, "markdown_blocks", flaky)
    assert _corpus(root) == [
        ("docs/a.md", 1, "internal-error", "RuntimeError: boom"),
        ("docs/b.md", 2, "undeclared-import", "otto.nope:B"),
    ]


def test_the_manifest_parses_and_names_no_missing_namespace():
    namespaces = load_manifest(CHECKOUT_ROOT / "api" / "public.toml")
    assert {"otto", "otto.lab", "otto.host", "otto.host.transfer", "otto.cli.registry"} <= set(
        namespaces
    )
    for name in namespaces:
        rel = name.replace(".", "/")
        assert (CHECKOUT_ROOT / "src" / f"{rel}.py").exists() or (
            CHECKOUT_ROOT / "src" / rel / "__init__.py"
        ).exists(), name


def test_report_mode_always_exits_zero_and_prints_a_summary(capsys):
    exit_code = teaching_main(
        ["--manifest", str(CHECKOUT_ROOT / "api" / "public.toml"), "--report"]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "api-surface-report:" in out.splitlines()[-1]


def _gating_tree(tmp_path, page_body):
    """A tiny repo whose ``otto`` exports ``Thing``, plus a manifest declaring ``otto``."""
    root = _repo(
        tmp_path / "repo",
        {
            "src/otto/__init__.py": '__all__ = ["Thing"]\nclass Thing:\n    pass\n',
            "docs/conf.py": 'doctest_global_setup = ""\n',
            "docs/page.md": page_body,
        },
    )
    manifest = tmp_path / "public.toml"
    manifest.write_text('[namespaces."otto"]\ntier = 1\nstability = "provisional"\n')
    return root, manifest


def test_gating_mode_exits_one_on_a_finding_under_the_given_root(tmp_path, capsys):
    root, manifest = _gating_tree(tmp_path, "```python\nfrom otto import Nope\n```\n")
    assert teaching_main(["--manifest", str(manifest), "--root", str(root)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert "docs/page.md:2: undeclared-import: otto:Nope" in out
    assert out[-1].startswith("api-surface-report: 0 agreement failure(s), 1 finding(s)")


def test_gating_mode_exits_zero_on_a_clean_tree(tmp_path, capsys):
    root, manifest = _gating_tree(tmp_path, "```python\nfrom otto import Thing\n```\n")
    assert teaching_main(["--manifest", str(manifest), "--root", str(root)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["api-surface-report: 0 agreement failure(s), 0 finding(s)"]


PRIVATE_DECL = StaticDeclaration(
    members={"otto.host": {"UnixHost", "Outer", "make_base"}, "otto": {"Thing"}},
    private={
        "otto.host:UnixHost": {"_connection_factory", "_login", "_run_put"},
        "otto.host:Outer": set(),
        "otto.host:Outer.Inner": {"_tick"},
        "otto:Thing": set(),
    },
)


def _private_findings(tmp_path, code):
    path = _page(tmp_path, f"```python\n{code}```\n")
    findings = check_file(path, PRIVATE_DECL, set(), repo_root=tmp_path, corpus={path})
    return [(f.kind, f.detail) for f in findings]


HEAD = "from otto.host import UnixHost\n"


@pytest.mark.parametrize(
    ("code", "detail"),
    [
        (
            HEAD + "class M(UnixHost):\n    def _login(self): pass\n",
            "otto.host:UnixHost._login: overridden",
        ),
        (
            HEAD + "class M(UnixHost):\n    async def _run_put(self): pass\n",
            "otto.host:UnixHost._run_put: overridden",
        ),
        (
            HEAD + "class M(UnixHost):\n    _connection_factory = object\n",
            "otto.host:UnixHost._connection_factory: overridden",
        ),
        (
            HEAD + "class M(UnixHost):\n    _login: int = 0\n",
            "otto.host:UnixHost._login: overridden",
        ),
        (
            HEAD + "UnixHost(_connection_factory=None)\n",
            "otto.host:UnixHost._connection_factory: passed as a constructor keyword",
        ),
        (HEAD + "h = UnixHost()\nh._run_put()\n", "otto.host:UnixHost._run_put: used"),
        (HEAD + "h: UnixHost = UnixHost()\nh._login = None\n", "otto.host:UnixHost._login: used"),
        (
            HEAD + "async def main():\n    async with UnixHost() as h:\n        await h._login()\n",
            "otto.host:UnixHost._login: used",
        ),
        (HEAD + "UnixHost._login\n", "otto.host:UnixHost._login: used"),
        (HEAD + "UnixHost()._login\n", "otto.host:UnixHost._login: used"),
        (HEAD + "getattr(UnixHost(), '_login')\n", "otto.host:UnixHost._login: used"),
        (
            HEAD + "class M(UnixHost):\n    def go(self):\n        return self._run_put()\n",
            "otto.host:UnixHost._run_put: used",
        ),
        (
            HEAD + "class M(UnixHost):\n    pass\nm = M()\nm._login()\n",
            "otto.host:UnixHost._login: used",
        ),
        (
            HEAD + "U = UnixHost\nclass M(U):\n    def _login(self): pass\n",
            "otto.host:UnixHost._login: overridden",
        ),
        (
            "import otto.host as oh\nclass M(oh.UnixHost):\n    def _login(self): pass\n",
            "otto.host:UnixHost._login: overridden",
        ),
        (
            "import otto.host as oh\nclass M(oh.Outer.Inner):\n    def _tick(self): pass\n",
            "otto.host:Outer.Inner._tick: overridden",
        ),
        (
            "from typing import Generic, TypeVar\n"
            + HEAD
            + "T = TypeVar('T')\nclass M(UnixHost[T]):\n    def _login(self): pass\n",
            "otto.host:UnixHost._login: overridden",
        ),
    ],
)
def test_taught_private_use_is_reported(tmp_path, code, detail):
    assert ("taught-private-member", detail) in _private_findings(tmp_path, code)


@pytest.mark.parametrize(
    "code",
    [
        # a reader's own helper, defined and called
        HEAD + "class M(UnixHost):\n    def _my_helper(self): pass\n"
        "    def go(self):\n        self._my_helper()\n",
        # supported dunders are public
        HEAD + "class M(UnixHost):\n    async def __aenter__(self): return self\n",
        # rebinding ends the instance
        HEAD + "h = UnixHost()\nh = object()\nh._login\n",
        # a class that is not otto's
        "class Base:\n    pass\nclass M(Base):\n    def _login(self): pass\n",
        # a staticmethod's first parameter is not an instance
        HEAD + "class M(UnixHost):\n    @staticmethod\n    def make(x):\n        return x._login\n",
        # a declared class with no underscore members
        "from otto import Thing\nclass M(Thing):\n    def _login(self): pass\n",
        # an instance bound inside a function or a class body does not leak
        HEAD + "def f():\n    h = UnixHost()\nh._login\n",
        HEAD + "class C:\n    h = UnixHost()\nh._login\n",
        # a class alias bound in a child scope does not leak either
        HEAD + "def f():\n    U = UnixHost\nclass M(U):\n    def _login(self): pass\n",
    ],
)
def test_untaught_or_own_names_are_not_reported(tmp_path, code):
    assert [k for k, _ in _private_findings(tmp_path, code) if k == "taught-private-member"] == []


def test_an_otto_base_the_checker_cannot_follow_fails_loudly(tmp_path):
    code = (
        HEAD + "def pick(c):\n    return c\nclass M(pick(UnixHost)):\n    def _login(self): pass\n"
    )
    assert ("unfollowable-otto-base", "pick(UnixHost)") in _private_findings(tmp_path, code)


def test_a_complex_base_with_no_otto_name_is_not_reported(tmp_path):
    code = "def pick(c):\n    return c\nclass M(pick(object)):\n    pass\n"
    assert _private_findings(tmp_path, code) == []


def _private_tree(tmp_path, page_body):
    root = _repo(
        tmp_path / "repo",
        {
            "src/otto/__init__.py": (
                '__all__ = ["Thing"]\nclass Thing:\n    def _hook(self): pass\n'
            ),
            "docs/conf.py": 'doctest_global_setup = ""\n',
            "docs/page.md": page_body,
        },
    )
    manifest = tmp_path / "public.toml"
    manifest.write_text('[namespaces."otto"]\ntier = 1\nstability = "provisional"\n')
    return root, manifest


def test_main_learns_the_private_members_from_the_producer(tmp_path, capsys):
    page = "```python\nfrom otto import Thing\nclass M(Thing):\n    def _hook(self): pass\n```\n"
    root, manifest = _private_tree(tmp_path, page)
    assert teaching_main(["--manifest", str(manifest), "--root", str(root)]) == 1
    assert "docs/page.md:4: taught-private-member: otto:Thing._hook: overridden" in (
        capsys.readouterr().out.splitlines()
    )


def test_main_fails_loudly_when_the_private_map_is_unavailable(tmp_path, capsys):
    root, manifest = _private_tree(tmp_path, "```python\nfrom otto import Thing\n```\n")
    (root / "src" / "otto" / "__init__.py").write_text("raise SystemExit(3)\n")
    assert teaching_main(["--manifest", str(manifest), "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert any(line.startswith("producer refusal: ") for line in out.splitlines())
    assert out.splitlines()[-1].endswith(", 1 producer refusal(s)")


def _with_generated(monkeypatch, generated):
    from scripts import api_regen

    monkeypatch.setattr(api_regen, "generate_worktree", lambda *a, **k: generated)


def test_producer_refusals_are_reported_even_when_a_private_map_survives(
    tmp_path, capsys, monkeypatch
):
    from scripts.api_regen import Generated

    root, manifest = _private_tree(tmp_path, "```python\nfrom otto import Thing\n```\n")
    _with_generated(monkeypatch, Generated(None, ["provenance: x"], {"otto:Thing": ["_hook"]}))
    assert teaching_main(["--manifest", str(manifest), "--root", str(root)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert "producer refusal: provenance: x" in out
    assert out[-1].endswith(", 1 producer refusal(s)")
    assert teaching_main(["--manifest", str(manifest), "--root", str(root), "--report"]) == 0
    assert "producer refusal: provenance: x" in capsys.readouterr().out.splitlines()


@pytest.mark.parametrize(
    "body",
    [
        "    if True:\n        def _login(self): pass\n",
        "    if False:\n        pass\n    elif True:\n        _login = 1\n",
        "    if False:\n        pass\n    else:\n        _login: int = 1\n",
        "    try:\n        pass\n    except OSError:\n        def _login(self): pass\n",
        (
            "    try:\n        pass\n    except OSError:\n        pass\n"
            "    else:\n        _login = 1\n"
        ),
        "    try:\n        pass\n    finally:\n        _login = 1\n",
        "    try:\n        _login = 1\n    except OSError:\n        pass\n",
        "    with open('x') as f:\n        def _login(self): pass\n",
        "    for i in range(2):\n        _login = i\n",
        "    for i in range(2):\n        pass\n    else:\n        _login = 1\n",
        "    while False:\n        _login = 1\n",
        "    while False:\n        pass\n    else:\n        _login = 1\n",
        "    match 1:\n        case 1:\n            def _login(self): pass\n",
        "    if True:\n        if True:\n            _login = 1\n",
    ],
)
def test_an_override_inside_a_compound_statement_is_reported(tmp_path, body):
    code = HEAD + "class M(UnixHost):\n" + body
    assert ("taught-private-member", "otto.host:UnixHost._login: overridden") in _private_findings(
        tmp_path, code
    )


@pytest.mark.parametrize(
    "body",
    [
        "    def go(self):\n        def _login(): pass\n",
        "    def go(self):\n        if True:\n            _login = 1\n",
        "    class Inner:\n        def _login(self): pass\n",
        "    if True:\n        class Inner:\n            _login = 1\n",
        "    f = lambda: [_login for _login in ()]\n",
    ],
)
def test_a_name_in_a_nested_scope_is_not_an_override_of_the_outer_class(tmp_path, body):
    code = HEAD + "class M(UnixHost):\n" + body
    assert [k for k, _ in _private_findings(tmp_path, code) if k == "taught-private-member"] == []


@pytest.mark.parametrize(
    ("code", "detail"),
    [
        # a nested class reached through an imported otto class
        (
            "from otto.host import Outer\nclass M(Outer.Inner):\n    def _tick(self): pass\n",
            "otto.host:Outer.Inner._tick: overridden",
        ),
        # a self-rebinding alias keeps what it was
        (
            HEAD + "U = UnixHost\nU = U\nclass M(U):\n    def _login(self): pass\n",
            "otto.host:UnixHost._login: overridden",
        ),
    ],
)
def test_taught_private_use_through_the_nested_and_rebound_forms(tmp_path, code, detail):
    assert ("taught-private-member", detail) in _private_findings(tmp_path, code)


@pytest.mark.parametrize(
    ("code", "base"),
    [
        (
            HEAD + "h = UnixHost()\nclass M(h.__class__):\n    def _login(self): pass\n",
            "h.__class__",
        ),
        (
            "from otto.host import Outer\nclass M(Outer.Missing):\n    pass\n",
            "Outer.Missing",
        ),
        (
            "from otto.host import make_base\nclass M(make_base()):\n    pass\n",
            "make_base()",
        ),
    ],
)
def test_every_unresolved_base_that_mentions_otto_fails_loudly(tmp_path, code, base):
    assert ("unfollowable-otto-base", base) in _private_findings(tmp_path, code)


def test_type_arguments_of_a_base_are_not_bases_and_produce_no_finding(tmp_path):
    code = HEAD + "class L(list[UnixHost]):\n    pass\n"
    assert _private_findings(tmp_path, code) == []


def test_code_below_a_closed_doctest_docstring_is_checked(tmp_path):
    # A def whose docstring holds a doctest is code throughout: the import below the
    # closed docstring is checked, not dropped as the doctest's output.
    page = _page(
        tmp_path,
        '```python\ndef add(a, b):\n    """Add.\n\n    >>> add(1, 2)\n    3\n    """\n'
        "    from otto.nope import Below\n    return a + b\n```\n",
    )
    assert _kinds(page, tmp_path) == [("undeclared-import", "otto.nope:Below", 8)]


def test_a_docstring_doctest_and_the_code_below_it_are_both_checked(tmp_path):
    page = _page(
        tmp_path,
        '```python\ndef add(a, b):\n    """Add.\n\n    >>> from otto.nope import Inside\n    """\n'
        "    from otto.nope import Below\n    return a + b\n```\n",
    )
    assert _kinds(page, tmp_path) == [
        ("undeclared-import", "otto.nope:Below", 7),
        ("undeclared-import", "otto.nope:Inside", 5),
    ]


def test_a_setup_only_name_below_a_closed_docstring_is_reported(tmp_path):
    page = _page(
        tmp_path,
        '```python\ndef f():\n    """\n    >>> f()\n    """\n    return LocalHost()\n```\n',
    )
    assert _kinds(page, tmp_path, setup={"LocalHost"}) == [("setup-only-name", "LocalHost", 6)]


def test_prose_inside_a_docstring_is_not_a_doctest_line(tmp_path):
    # A blank line ends the doctest run; a prose line that starts with "..." opens nothing.
    page = _page(
        tmp_path,
        '```python\ndef f():\n    """Do it.\n\n    >>> f()\n\n'
        '    ... and LocalHost is prose here\n    """\n    return 1\n```\n',
    )
    assert _kinds(page, tmp_path, setup={"LocalHost"}) == []


def test_an_f_string_holding_a_prompt_reads_alike_on_every_interpreter(tmp_path):
    # 3.12+ tokenizes an f-string as start/middle/end tokens, 3.10 as one STRING.
    page = _page(
        tmp_path,
        '```python\nx = f"""\n>>> from otto.nope import Inside\n"""\n'
        "from otto.nope import Below\n```\n",
    )
    assert _kinds(page, tmp_path) == [
        ("undeclared-import", "otto.nope:Below", 5),
        ("undeclared-import", "otto.nope:Inside", 3),
    ]


def test_an_untokenizable_transcript_is_still_read_as_a_transcript(tmp_path):
    # An output line with an unbalanced quote stops the tokenizer; the block keeps
    # the transcript reading, so its output is never taken for code.
    page = _page(tmp_path, "```python\n>>> import os\n'unbalanced\nfrom otto.nope import X\n```\n")
    assert _kinds(page, tmp_path) == []


def test_a_bad_manifest_is_a_fail_line_not_a_traceback(tmp_path, capsys):
    manifest = tmp_path / "public.toml"
    manifest.write_text("not = [valid\n", encoding="utf-8")
    for mode in ([], ["--report"]):
        assert teaching_main(["--manifest", str(manifest), *mode]) == 1
        (line,) = capsys.readouterr().out.splitlines()
        assert line.startswith("FAIL not valid TOML: ")


def test_a_missing_manifest_is_a_fail_line_not_a_traceback(tmp_path, capsys):
    absent = tmp_path / "absent.toml"
    assert teaching_main(["--manifest", str(absent)]) == 1
    assert capsys.readouterr().out == f"FAIL {absent} does not exist\n"


def test_a_failed_namespace_report_is_a_fail_line_not_a_traceback(tmp_path, capsys, monkeypatch):
    from scripts import api_agreement

    def broken(namespaces, repo):
        raise api_agreement.AgreementError("the reporting child died")

    monkeypatch.setattr(api_agreement, "namespace_reports", broken)
    root, manifest = _gating_tree(tmp_path, "```python\nfrom otto import Thing\n```\n")
    for mode in ([], ["--report"]):
        assert teaching_main(["--manifest", str(manifest), "--root", str(root), *mode]) == 1
        assert capsys.readouterr().out == (
            "FAIL cannot report the declared namespaces: the reporting child died\n"
        )


SHAPES = (
    "# Shape canary\n"
    "\n"
    "```python\n"
    "from otto.nope import Plain\n"
    "```\n"
    "\n"
    "Indented block:\n"
    "\n"
    "    from otto.nope import Indented\n"
    "\n"
    "~~~python\n"
    "from otto.nope import Tilde\n"
    "~~~\n"
    "\n"
    "- bullet text:\n"
    "\n"
    "  ```python\n"
    "  from otto.nope import ListItem\n"
    "  ```\n"
    "\n"
    "```{code-block} python\n"
    "from otto.nope import Directive\n"
    "```\n"
    "\n"
    "```{note}\n"
    "```python\n"
    "from otto.nope import EqualNested\n"
    "```\n"
    "```\n"
    "\n"
    "````{note}\n"
    "```python\n"
    "from otto.nope import WiderNested\n"
    "```\n"
    "````\n"
)


def test_every_fence_shape_the_docs_use_reaches_the_validator(tmp_path):
    """Each code-block shape a page uses is checked, each with its own finding.

    Carried over from the v1 docs walk's shape canary: a plain fence, a
    CommonMark indented block, a tilde fence, a fence indented in a list item,
    MyST's ``{code-block}``, and a fence nested in an equal-count and in a
    wider container. Distinct names, so a shape that goes quiet cannot hide
    behind another.
    """
    page = _page(tmp_path, SHAPES, name="shapes.md")
    assert _kinds(page, tmp_path) == [
        ("undeclared-import", "otto.nope:Plain", 4),
        ("undeclared-import", "otto.nope:Indented", 9),
        ("undeclared-import", "otto.nope:Tilde", 12),
        ("undeclared-import", "otto.nope:ListItem", 18),
        ("undeclared-import", "otto.nope:Directive", 22),
        ("undeclared-import", "otto.nope:EqualNested", 27),
        ("undeclared-import", "otto.nope:WiderNested", 33),
    ]


def test_a_parenthesized_import_is_checked_and_an_otto_lookalike_is_not(tmp_path):
    """Carried over from the v1 docs walk: multi-line imports, plain and doctest.

    ``otto_something`` is another package, never a broken ``otto`` import.
    """
    page = _page(
        tmp_path,
        "```python\nfrom otto.host import (\n    Element,\n    Missing,\n)\n"
        "from otto_something import Thing\nimport otto_something\n```\n"
        "\n```{doctest}\n>>> from otto.host import (\n...     LocalHost,\n"
        "...     AlsoMissing,\n... )\n>>> LocalHost is not None\nTrue\n```\n",
    )
    assert _kinds(page, tmp_path) == [
        ("undeclared-import", "otto.host:Missing", 2),
        ("undeclared-import", "otto.host:AlsoMissing", 11),
    ]


WITHDRAWN_DECL = StaticDeclaration(
    members={"otto.host": {"UnixHost", "Outer"}, "otto": {"Thing"}},
    private={"otto.host:UnixHost": set(), "otto.host:Outer": set(), "otto:Thing": set()},
    withdrawn={"otto.host:UnixHost"},
)


def test_a_taught_construction_of_a_withdrawn_class_is_a_finding(tmp_path):
    path = _page(
        tmp_path,
        "```python\nfrom otto.host import UnixHost, Outer\nOuter()\nUnixHost()\n```\n",
    )
    findings = check_file(path, WITHDRAWN_DECL, set(), repo_root=tmp_path, corpus={path})
    assert [(f.line, f.render().split(": ", 2)[2]) for f in findings] == [
        (4, "constructs otto.host:UnixHost, whose constructor is withdrawn (construct = false)")
    ]


def test_a_withdrawn_class_is_not_a_finding_when_only_named(tmp_path):
    path = _page(
        tmp_path,
        "```python\nfrom otto.host import UnixHost\nclass M(UnixHost):\n    pass\n```\n",
    )
    assert check_file(path, WITHDRAWN_DECL, set(), repo_root=tmp_path, corpus={path}) == []


def test_a_local_subclass_construction_exercises_the_otto_base_constructor(tmp_path):
    path = _page(
        tmp_path,
        "```python\nfrom otto.host import UnixHost\nclass M(UnixHost):\n    pass\nM()\n```\n",
    )
    constructed = set()
    findings = check_file(
        path, WITHDRAWN_DECL, set(), repo_root=tmp_path, corpus={path}, constructed=constructed
    )
    assert [(f.line, f.kind, f.detail) for f in findings] == [
        (
            5,
            "withdrawn-constructor",
            "constructs otto.host:UnixHost, whose constructor is withdrawn (construct = false)",
        )
    ]
    assert constructed == {"otto.host:UnixHost"}


def test_untaught_constructors_lists_only_never_constructed_classes(tmp_path, capsys):
    root = _repo(
        tmp_path / "repo",
        {
            "src/otto/__init__.py": (
                '__all__ = ["Thing", "Other", "Gone", "Sub", "Proto", "OttoError", "Boom", "Hue"]\n'
                "import enum\nfrom typing import Protocol\nfrom otto.errors import OttoError\n"
                "class Hue(enum.Enum):\n    RED = 1\n"
                "class Thing:\n    pass\nclass Other:\n    pass\nclass Gone:\n    pass\n"
                "class Proto(Protocol):\n    def f(self) -> None: ...\n"
                "class Sub(Thing):\n    pass\nclass Boom(OttoError):\n    pass\n"
            ),
            "src/otto/errors.py": (
                '__all__ = ["OttoError"]\nclass OttoError(Exception):\n    pass\n'
            ),
            "docs/conf.py": 'doctest_global_setup = ""\n',
            "docs/page.md": "```python\nfrom otto import Thing\nThing()\n```\n",
        },
    )
    manifest = tmp_path / "public.toml"
    manifest.write_text(
        '[namespaces."otto"]\ntier = 1\nstability = "provisional"\n'
        '[namespaces."otto.errors"]\ntier = 1\nstability = "provisional"\n'
        '[symbols."otto:Gone"]\nconstruct = false\nwhy = "built by otto only"\n'
    )
    argv = ["--manifest", str(manifest), "--root", str(root), "--untaught-constructors"]
    assert teaching_main(argv) == 0
    assert capsys.readouterr().out.splitlines() == ["otto:Other", "otto:Sub"]


def test_untaught_constructors_on_a_refused_dump_prints_refusals_only(tmp_path, capsys):
    root = _repo(
        tmp_path / "repo",
        {
            "src/otto/__init__.py": '__all__ = ["Thing"]\nclass Thing:\n    pass\n',
            "docs/conf.py": 'doctest_global_setup = ""\n',
            "docs/page.md": "text\n",
        },
    )
    manifest = tmp_path / "public.toml"
    manifest.write_text(
        '[namespaces."otto"]\ntier = 1\nstability = "provisional"\n'
        '[symbols."otto:Nope"]\nsubclass = false\nwhy = "w"\n'
    )
    argv = ["--manifest", str(manifest), "--root", str(root), "--untaught-constructors"]
    assert teaching_main(argv) == 1
    assert capsys.readouterr().out.splitlines() == [
        "producer refusal: otto:Nope: symbol entry names no public binding or member"
    ]


# -- the positional rule (#982, #983) -----------------------------------------


def test_a_positional_constructor_call_beyond_the_shape_is_a_finding(tmp_path):
    page = _page(
        tmp_path, "```python\nfrom otto import Result, Status\nResult(Status.Success)\n```\n"
    )
    assert _kinds(page, tmp_path) == [
        ("positional-call", "otto:Result: 1 positional where it accepts 0 arguments", 3)
    ]


def test_a_keyword_constructor_call_is_not_a_finding(tmp_path):
    page = _page(
        tmp_path, "```python\nfrom otto import Result, Status\nResult(status=Status.Success)\n```\n"
    )
    assert _kinds(page, tmp_path) == []


def test_a_from_imported_function_called_past_its_shape_is_a_finding(tmp_path):
    page = _page(tmp_path, '```python\nfrom otto import expect\nexpect(cond, "why")\n```\n')
    assert _kinds(page, tmp_path) == [
        ("positional-call", "otto:expect: 2 positional where it accepts 1 argument", 3)
    ]
    fine = _page(
        tmp_path, '```python\nfrom otto import expect\nexpect(cond, msg="why")\n```\n', "b.md"
    )
    assert _kinds(fine, tmp_path) == []


def test_a_module_attribute_call_and_a_method_call_are_judged(tmp_path):
    page = _page(
        tmp_path,
        "```python\nimport otto\nfrom otto.host import LocalHost\n"
        'otto.expect(c, "w")\nh = LocalHost()\nh.run("ls", 5)\nLocalHost().run("ls", 5)\n```\n',
    )
    assert _kinds(page, tmp_path) == [
        ("positional-call", "otto:expect: 2 positional where it accepts 1 argument", 4),
        ("positional-call", "otto.host:LocalHost.run: 2 positional where it accepts 1 argument", 6),
        ("positional-call", "otto.host:LocalHost.run: 2 positional where it accepts 1 argument", 7),
    ]


def test_an_unknown_shape_a_splat_and_an_unresolved_callee_are_not_judged(tmp_path):
    page = _page(
        tmp_path,
        "```python\nfrom otto.host import Element, LocalHost\nfrom otto import expect\n"
        'Element("a", "b", "c")\nexpect(*args)\nsomething("a", "b")\nh = LocalHost()\n'
        'h.other("a", "b")\n```\n',
    )
    assert _kinds(page, tmp_path) == []


def test_a_prose_span_teaching_a_positional_call_is_a_finding(tmp_path):
    page = _page(
        tmp_path,
        'Intro.\n\nCall `expect(cond, "why")` to record it, or `expect(cond, msg="why")`.\n'
        "A nested one: ``Result(Status.Success)`` in RST ticks.\n",
    )
    assert _kinds(page, tmp_path) == [
        ("positional-call", "otto:expect: 2 positional where it accepts 1 argument", 3),
        ("positional-call", "otto:Result: 1 positional where it accepts 0 arguments", 4),
    ]


def test_a_prose_span_resolves_a_class_method_and_ignores_what_it_cannot(tmp_path):
    page = _page(
        tmp_path,
        'Use `LocalHost.run(h, "ls", 5)` or `LocalHost().run("ls", 5)`; '
        '`load_lab("x", 1)` has two homes with one shape, '
        '`nothing("a", "b")` is undeclared, '
        '`expect(cond[, "why"])` is not Python, and `host.run("ls", 5)` names no class.\n',
    )
    assert _kinds(page, tmp_path) == [
        ("positional-call", "otto.host:LocalHost.run: 3 positional where it accepts 1 argument", 1),
        ("positional-call", "otto.host:LocalHost.run: 2 positional where it accepts 1 argument", 1),
        ("positional-call", "otto:load_lab: 2 positional where it accepts 1 argument", 1),
    ]


def test_a_call_through_the_class_may_pass_the_receiver(tmp_path):
    page = _page(
        tmp_path,
        "```python\nfrom otto.host import LocalHost\nh = LocalHost()\n"
        'LocalHost.run(h, "ls")\nLocalHost.run(h, "ls", 5)\n```\n\n'
        'Prose: `LocalHost.run(h, "ls")` and `LocalHost.run("ls", 5)`.\n',
    )
    assert _kinds(page, tmp_path) == [
        ("positional-call", "otto.host:LocalHost.run: 3 positional where it accepts 1 argument", 5)
    ]


def test_a_prose_span_inside_a_container_is_read(tmp_path):
    page = _page(
        tmp_path,
        '```{note}\nCall `expect(cond, "why")` here.\n```\n\n'
        ":::{tip}\nOr `Result(Status.Success)`.\n:::\n\n"
        '```python\nexpect(cond, "why")  # a fence line: `x()` spans here are not spans\n```\n',
    )
    assert _kinds(page, tmp_path) == [
        ("positional-call", "otto:expect: 2 positional where it accepts 1 argument", 2),
        ("positional-call", "otto:Result: 1 positional where it accepts 0 arguments", 6),
    ]


def test_a_page_defined_override_is_not_judged_against_the_base(tmp_path):
    page = _page(
        tmp_path,
        "```python\nfrom otto.host import LocalHost\n"
        "class Mine(LocalHost):\n    def run(self, cmd, timeout):\n        pass\n"
        'm = Mine()\nm.run("ls", 5)\nm.sftp("a", "b")\n```\n',
    )
    decl = StaticDeclaration(
        members=DECL.members,
        private=DECL.private,
        shapes={**DECL.shapes, "otto.host:LocalHost.sftp": 0},
        receivers=DECL.receivers,
    )
    findings = check_file(page, decl, set(), repo_root=tmp_path, corpus={page})
    assert [(f.kind, f.detail, f.line) for f in findings] == [
        (
            "positional-call",
            "otto.host:LocalHost.sftp: 2 positional where it accepts 0 arguments",
            8,
        )
    ]


def test_a_prose_span_follows_the_page_s_own_imports_first(tmp_path):
    page = _page(
        tmp_path,
        '```python\nfrom otto.host import LocalHost as LH\n```\n\nThen `LH().run("ls", 5)`.\n',
    )
    assert _kinds(page, tmp_path) == [
        ("positional-call", "otto.host:LocalHost.run: 2 positional where it accepts 1 argument", 5)
    ]


def test_a_span_inside_a_fence_is_not_read_twice(tmp_path):
    page = _page(tmp_path, "```python\nfrom otto import expect\nexpect(cond, `x`)\n```\n")
    kinds = [k for k, _, _ in _kinds(page, tmp_path)]
    assert "positional-call" not in kinds


def test_positional_shapes_read_the_dump():
    dump = (
        "# api-snapshot v2\n# producer-schema 1\n"
        "name\totto:f\tfunction\ncall\totto:f\tsync\tPK:a:- KO:b:N\n"
        "name\totto:g\tfunction\ncall\totto:g\tsync\tPK:a:- VP:rest:-\n"
        "name\totto:C\tclass\nmro\totto:C\t-\ncall\totto:C\tsync\t@manifest:construct=false\n"
        "abstract\totto:C\t-\nmember\totto:C.m\tmethod\tsync\tPO:x:- PK:y:- KO:z:N\n"
        "member\totto:C.k\tclassmethod\tsync\tKO:n:N\n"
        "name\totto:E\tclass\nmro\totto:E\t@builtin:builtins.ValueError\n"
        "call\totto:E\tsync\t@builtin:builtins.ValueError\nabstract\totto:E\t-\n"
    )
    assert api_teaching.positional_shapes(dump) == (
        {"otto:f": 1, "otto:g": None, "otto:C": None, "otto:C.m": 2, "otto:C.k": 0, "otto:E": None},
        {"otto:C.m"},
    )


def test_an_elided_argument_teaches_no_position(tmp_path):
    page = _page(
        tmp_path,
        "```python\nfrom otto import Result\nResult(..., status=1)\n```\n\n"
        "Prose: `Result(..., status=1)` and `Result(x, ...)`.\n",
    )
    assert _kinds(page, tmp_path) == [
        ("positional-call", "otto:Result: 1 positional where it accepts 0 arguments", 6)
    ]


def test_a_page_defined_subclass_has_its_own_constructor(tmp_path):
    page = _page(
        tmp_path,
        "```python\nfrom otto.host import LocalHost\n"
        "class Mine(LocalHost):\n    def __init__(self, a, b):\n        pass\n"
        'm = Mine(1, 2)\nm.run("ls", 5)\n```\n',
    )
    assert _kinds(page, tmp_path) == [
        ("positional-call", "otto.host:LocalHost.run: 2 positional where it accepts 1 argument", 7)
    ]


def test_a_bare_prose_name_that_is_also_a_method_name_is_ambiguous(tmp_path):
    # ``deploy`` is a declared function taking no positional argument AND a
    # LocalHost method taking one: the span could mean either, so it is not judged.
    decl = StaticDeclaration(
        members=DECL.members,
        private=DECL.private,
        shapes={
            **DECL.shapes,
            "otto.docker:deploy": 0,
            "otto.host:LocalHost.deploy": 1,
            "otto.host:LocalHost.sftp": 0,
        },
        receivers=DECL.receivers,
    )
    # ``sftp(1)`` is a manpage reference: a method of that name is not a binding.
    page = _page(tmp_path, "Then `deploy(host)` runs it, as `sftp(1)` would.\n")
    findings = check_file(page, decl, set(), repo_root=tmp_path, corpus={page})
    assert findings == []
    assert decl.bindings_named("deploy") == ["otto.docker:deploy"]
    assert decl.methods_named("deploy") == ["otto.host:LocalHost.deploy"]


def test_a_classmethod_called_through_the_class_gets_no_receiver_slack(tmp_path):
    page = _page(
        tmp_path,
        "```python\nfrom otto import Results\nResults.collect(items)\n```\n\n"
        "Prose: `Results.collect(items)`.\n",
    )
    assert _kinds(page, tmp_path) == [
        ("positional-call", "otto:Results.collect: 1 positional where it accepts 0 arguments", 3),
        ("positional-call", "otto:Results.collect: 1 positional where it accepts 0 arguments", 6),
    ]


def test_a_bound_instance_applied_is_its_class_s_call(tmp_path):
    page = _page(
        tmp_path,
        "```python\nfrom otto.suite import ExpectCollector\ncollector = ExpectCollector()\n"
        'collector(cond, "why")\nExpectCollector()(cond, "why")\ncollector(cond, msg="why")\n```\n',
    )
    assert _kinds(page, tmp_path) == [
        (
            "positional-call",
            "otto.suite:ExpectCollector.__call__: 2 positional where it accepts 1 argument",
            4,
        ),
        (
            "positional-call",
            "otto.suite:ExpectCollector.__call__: 2 positional where it accepts 1 argument",
            5,
        ),
    ]


def test_a_prose_name_the_page_bound_itself_is_what_the_page_says(tmp_path):
    page = _page(
        tmp_path,
        "```python\ndef Result(value):\n    return value\n```\n\nThen `Result(1)` is its own.\n",
    )
    assert _kinds(page, tmp_path) == []


def test_prose_is_judged_in_page_order_with_the_blocks(tmp_path):
    page = _page(
        tmp_path,
        '```python\nfrom otto import load_lab as make\n```\n\nHere `make("lab")` is fine.\n\n'
        '```python\nfrom otto import Result as make\n```\n\nNow `make("lab")` is a constructor.\n',
    )
    assert _kinds(page, tmp_path) == [
        ("positional-call", "otto:Result: 1 positional where it accepts 0 arguments", 11)
    ]


def test_a_markdown_display_fence_is_neither_code_nor_prose(tmp_path):
    page = _page(
        tmp_path,
        "````markdown\nShown: `Result(1)` and a fence:\n```python\nResult(1)\n```\n````\n\n"
        "But `Result(1)` here is prose.\n",
    )
    assert _kinds(page, tmp_path) == [
        ("positional-call", "otto:Result: 1 positional where it accepts 0 arguments", 8)
    ]


def test_a_span_with_an_invalid_escape_is_still_judged():
    # Under warnings-as-errors ``ast.parse`` would raise on the escape; the shared
    # quiet parser reads it like a code block.
    expr = api_teaching._try_parse_expression('Result("\\d")')
    assert expr is not None
    assert expr.func.id == "Result"
