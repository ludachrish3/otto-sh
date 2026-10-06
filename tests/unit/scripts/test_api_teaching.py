"""``scripts/api_teaching.py``: every otto name the docs and shipped examples teach is declared."""

import pytest

from scripts import api_teaching
from scripts.api_manifest import load_manifest
from scripts.api_teaching import StaticDeclaration, check_corpus, check_file, setup_otto_names
from scripts.api_teaching import main as teaching_main
from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

DECL = StaticDeclaration(
    members={
        "otto": {"Status", "load_lab"},
        "otto.docker": {"deploy"},
        "otto.host": {"Element", "LocalHost", "host_identity"},
        "otto.examples": set(),
    }
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


def test_preview_manifest_parses_and_names_no_missing_namespace():
    namespaces = load_manifest(PROJECT_ROOT / "scripts" / "api_public_preview.toml")
    assert "otto.lab" not in namespaces  # created by P1, not before
    assert {"otto", "otto.host", "otto.host.transfer", "otto.cli.registry"} <= set(namespaces)
    for name in namespaces:
        rel = name.replace(".", "/")
        assert (PROJECT_ROOT / "src" / f"{rel}.py").exists() or (
            PROJECT_ROOT / "src" / rel / "__init__.py"
        ).exists(), name


def test_report_mode_always_exits_zero_and_prints_a_summary(capsys):
    exit_code = teaching_main(
        ["--manifest", str(PROJECT_ROOT / "scripts" / "api_public_preview.toml"), "--report"]
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
