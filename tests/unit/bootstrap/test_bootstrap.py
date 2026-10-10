"""bootstrap(): phases, idempotence, containment framing."""

import pathlib
import textwrap
import types

import pytest

from otto import bootstrap as bs
from tests._fixtures.sutrepo import make_sut_repo


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """Reset bootstrap's caches around each test.

    What a test's init module registers, providers included, is restored by the
    root conftest's ``_isolate_registries``. That matters here: the D2 check
    reads the provider subscriptions on EVERY ``bootstrap()``, so a provider
    leaked by one test, owned by a repo name a later test happens to reuse,
    would fail that later test's bootstrap for a repo it never registered
    anything from.
    """
    bs._reset()
    yield
    bs._reset()


def _write_repo(tmp_path, *, broken_test: bool = False) -> str:
    files = (
        {"tests/test_broken.py": "def broken(:\n"}  # SyntaxError
        if broken_test
        else {"tests/test_ok.py": "X = 1\n"}
    )
    return str(make_sut_repo(tmp_path / "repo", name="repo", tests=["tests"], files=files))


def test_idempotent_single_result(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo(tmp_path))
    first = bs.bootstrap()
    assert first is bs.bootstrap()
    assert first.errors == []
    assert len(first.repos) == 1


def test_bootstrap_never_imports_a_test_file(tmp_path, monkeypatch):
    """No otto code executes a test file outside a pytest session (spec §5.6).

    The repo's test file raises a ``SyntaxError`` if it is ever imported, and
    writes a sentinel before that; a completed bootstrap has neither.
    """
    sentinel = tmp_path / "imported"
    body = f"import pathlib\npathlib.Path({str(sentinel)!r}).write_text('x')\ndef broken(:\n"
    repo = make_sut_repo(
        tmp_path / "repo", name="repo", tests=["tests"], files={"tests/test_broken.py": body}
    )
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
    assert bs.bootstrap().errors == []
    assert not sentinel.exists()


def test_discover_runs_no_user_code(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo(tmp_path, broken_test=True))
    discovered = bs.discover()  # broken test file must NOT explode discovery
    assert len(discovered.repos) == 1
    assert discovered.errors == []  # phase 1 never imports it, so it cannot have failed yet


def _write_bad_toml_repo(tmp_path) -> str:
    repo = tmp_path / "bad"
    (repo / ".otto").mkdir(parents=True)
    (repo / ".otto" / "settings.toml").write_text(  # sutrepo-exempt: malformed TOML IS the subject
        "this is [not valid toml\n"
    )
    return str(repo)


def test_malformed_settings_toml_is_contained_and_framed(tmp_path, monkeypatch):
    good = _write_repo(tmp_path)
    bad = _write_bad_toml_repo(tmp_path)
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{good},{bad}")
    result = bs.bootstrap()
    assert len(result.repos) == 1  # the healthy repo still loads
    assert len(result.errors) == 1
    msg = str(result.errors[0])
    assert "failed to load" in msg
    assert "settings.toml" in msg
    assert str(bad) in msg


def test_discover_contains_settings_errors_without_raising(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_bad_toml_repo(tmp_path))
    discovered = bs.discover()  # malformed config data must NOT explode discovery
    assert discovered.repos == []
    # The name of this test promises the error is CONTAINED, not merely survived;
    # asserting only `repos == []` passed just as well when nothing was recorded.
    assert len(discovered.errors) == 1
    assert "settings.toml" in str(discovered.errors[0])


def test_invalidate_recovers_from_fixed_repo(tmp_path, monkeypatch):
    """The embedder recovery path (todo/bootstrap-discovery-errors-accumulate.md):
    fix the repo, invalidate(), re-bootstrap — the stale error is gone because
    errors ride the cached ``DiscoveryResult`` instead of a parallel global."""
    bad = _write_bad_toml_repo(tmp_path)
    monkeypatch.setenv("OTTO_SUT_DIRS", bad)
    assert bs.bootstrap().errors
    # Fix the same repo in place, then invalidate: recomputed discovery must
    # not carry the prior failure.
    fixed_toml = 'name = "fixed"\nversion = "1.0.0"\n'
    (pathlib.Path(bad) / ".otto" / "settings.toml").write_text(  # sutrepo-exempt: in-place repair
        fixed_toml
    )
    bs.invalidate()
    assert bs.bootstrap().errors == []


def test_reset_clears_discovery_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_bad_toml_repo(tmp_path))
    assert bs.bootstrap().errors
    bs._reset()
    # A re-bootstrap against a now-healthy world must not carry stale errors.
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo(tmp_path))
    assert bs.bootstrap().errors == []


# The two documented ways a pytest module declines to load, plus the assertion
# flavour. All three raise BaseException subclasses that are NOT Exception, so a
# containment seam filtering on `except Exception` lets them brick every command.
DECLINING_MODULE_BODIES = [
    ("importorskip", "import pytest\npytest.importorskip('otto_no_such_optional_dep')\n"),
    ("skipmodule", "import pytest\npytest.skip('needs a dep', allow_module_level=True)\n"),
    ("failnotrace", "import pytest\npytest.fail('cannot load', pytrace=False)\n"),
]


# Exceptions a containment seam must NEVER swallow. `SyncPhaseInterrupt` is here
# deliberately: it subclasses KeyboardInterrupt precisely so the signal contract
# survives generic handling, and this pins that it still does.
PROPAGATING_BODIES = [
    ("kbint", "raise KeyboardInterrupt()\n", KeyboardInterrupt),
    ("sysexit", "raise SystemExit(3)\n", SystemExit),
    ("genexit", "raise GeneratorExit()\n", GeneratorExit),
    (
        "syncphase",
        "from otto.lifecycle import SyncPhaseInterrupt\nraise SyncPhaseInterrupt(2)\n",
        KeyboardInterrupt,
    ),
]


@pytest.mark.parametrize(("stem", "body", "expected"), PROPAGATING_BODIES)
def test_interrupt_and_exit_still_propagate(tmp_path, monkeypatch, stem, body, expected):
    """Widening the seam must not eat Ctrl-C, process exit, or otto's signal type.

    Containing these would be worse than the bug being fixed: a user pressing
    Ctrl-C while init modules load would see their interrupt turned into a
    framed 'failed to load' line and otto would carry on.
    """
    repo = _write_repo_with_init_module(tmp_path, f"init{stem}", body)
    monkeypatch.setenv("OTTO_SUT_DIRS", repo)
    with pytest.raises(expected):
        bs.bootstrap()


def test_cancelled_error_is_contained_not_propagated(tmp_path, monkeypatch):
    """``asyncio.CancelledError`` is framed, and that omission is deliberate.

    It is absent from ``otto.errors.UNCONTAINABLE`` because it cannot reach a
    seam wrapping a synchronous ``importlib`` call made from the console entry
    before any event loop exists — and naming it would import ``asyncio`` into
    the composition root, which neither ``otto.errors`` nor ``otto.bootstrap``
    pulls today (``tests/unit/import_budget`` measures that surface). A module
    body that raises it is just user code failing. This pins the decision so it
    cannot be quietly reversed in either direction.
    """
    import asyncio

    body = "import asyncio\nraise asyncio.CancelledError()\n"
    repo = _write_repo_with_init_module(tmp_path, "initcancelled", body)
    monkeypatch.setenv("OTTO_SUT_DIRS", repo)
    result = bs.bootstrap()
    assert len(result.errors) == 1
    # Assert the TYPE, not `isinstance(..., BaseException)`: BootstrapError.__init__
    # always assigns a BaseException to __cause__, so the broad form passes for any
    # contained exception and pins nothing.
    assert isinstance(result.errors[0].__cause__, asyncio.CancelledError)


def _write_repo_with_init_module(tmp_path, stem: str, body: str) -> str:
    """A repo whose single ``init`` module runs *body* — bootstrap's OTHER user-code seam."""
    repo = make_sut_repo(
        tmp_path / stem,
        name=stem,
        extra=f'libs = ["lib"]\ninit = ["{stem}_init"]\n',
        files={f"lib/{stem}_init.py": textwrap.dedent(body)},
    )
    return str(repo)


@pytest.mark.parametrize(("stem", "body"), DECLINING_MODULE_BODIES)
def test_module_level_pytest_outcome_in_init_module_is_contained(tmp_path, monkeypatch, stem, body):
    """A module that DECLINES to load must not traceback out of bootstrap.

    ``settings.toml``'s ``init`` list names user modules that register hosts,
    products and ``@instruction`` commands. ``pytest.importorskip`` is the
    mainstream idiom for an optional dependency, and all three bodies raise
    ``BaseException`` subclasses that are not ``Exception``, so reverting the
    seam to ``except Exception`` would let one such module brick every command.
    """
    repo = _write_repo_with_init_module(tmp_path, f"init{stem}", body)
    monkeypatch.setenv("OTTO_SUT_DIRS", repo)
    try:
        result = bs.bootstrap()
    except BaseException as exc:  # the escape IS the defect under test
        raise AssertionError(
            f"bootstrap() let {type(exc).__name__} escape the init-module seam: {exc!r}"
        ) from exc
    assert len(result.errors) == 1
    assert "failed to load" in str(result.errors[0])


def test_init_module_imports_run_under_the_registering_repo_marker(tmp_path, monkeypatch):
    """Phase-2 imports must be attributable to the repo whose imports they are.

    Registration seams read ``otto.registry.get_registering_repo()`` to record
    WHICH repo registered an entry; only bootstrap can tell them, and only
    while the import is on the stack. Observed from inside the init module
    itself — asserting it after ``bootstrap()`` returns would pass just as well
    with the marker never set at all.
    """
    seen = tmp_path / "seen.txt"
    body = f"""
        import pathlib

        from otto.registry import get_registering_repo

        pathlib.Path({str(seen)!r}).write_text(repr(get_registering_repo()))
    """
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo_with_init_module(tmp_path, "marked", body))
    result = bs.bootstrap()
    assert result.errors == []
    assert seen.read_text() == "'marked'"
    # And the marker is bootstrap-scoped: nothing after it inherits the name.
    from otto.registry import get_registering_repo

    assert get_registering_repo() is None


def test_discovery_seam_contains_a_base_exception(tmp_path, monkeypatch):
    """The phase-1 seam is defensive, and defensive code still needs a guard.

    ``Repo(sut_dir=...)`` only parses TOML today, so nothing naturally raises a
    non-``Exception`` there — which is exactly why reverting it to ``except
    Exception`` was invisible to the suite. Inject one.
    """
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo(tmp_path))
    import otto.config.repo as repo_mod

    def _raise_skipped(*_a, **_k):
        pytest.skip("simulated declining repo", allow_module_level=True)

    monkeypatch.setattr(repo_mod, "Repo", _raise_skipped)
    try:
        discovered = bs.discover()
    except BaseException as exc:  # the escape IS the defect under test
        raise AssertionError(
            f"discover() let {type(exc).__name__} escape the phase-1 seam: {exc!r}"
        ) from exc
    assert discovered.repos == []
    assert len(discovered.errors) == 1
    assert "settings.toml" in str(discovered.errors[0])


def test_composition_root_does_not_import_asyncio():
    """The reason ``CancelledError`` is not in ``UNCONTAINABLE`` — pinned, not asserted in prose."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", "import otto.bootstrap, sys; print('asyncio' in sys.modules)"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False", (
        "otto.bootstrap now pulls asyncio; the import-budget argument in "
        "otto.errors.UNCONTAINABLE's docstring is stale — revisit whether "
        "asyncio.CancelledError should be listed."
    )


# --- D2: a repo that registers providers must declare the labs it applies to ---


def _write_provider_repo(tmp_path, stem: str, *, project: str = "", seam: str = "product") -> str:
    """A repo whose init module registers a real provider on *seam*, plus *project* TOML.

    The registration goes through ``register_*_provider`` rather than poking the
    module-level list, because the owner name D2 reads is captured THERE, from
    the ``registering_repo`` marker bootstrap sets around the init import. A
    fixture appending to the list directly would carry ``None`` for the owner
    and every check below would pass vacuously.
    """
    register = {
        "product": "from otto.host.product import register_product_provider as register",
        "dev_tool": "from otto.host.dev_tool import register_dev_tool_provider as register",
    }[seam]
    repo = make_sut_repo(
        tmp_path / stem,
        name=stem,
        extra=f'libs = ["lib"]\ninit = ["{stem}_init"]\n' + textwrap.dedent(project),
        files={f"lib/{stem}_init.py": f"{register}\n\nregister(lambda host: [])\n"},
    )
    return str(repo)


def _assert_names_the_missing_declaration(msg: str, stem: str) -> None:
    """The refusal must name the repo AND spell the escape hatch verbatim."""
    assert stem in msg  # which repo — a fleet has a dozen
    assert "lab_patterns" in msg
    assert "[project]" in msg
    assert 'lab_patterns = [".*"]' in msg  # the explicit match-all, copy-pasteable


def test_provider_registering_repo_without_lab_patterns_fails_bootstrap(tmp_path, monkeypatch):
    """D2: registering a provider makes ``[project] lab_patterns`` mandatory."""
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_provider_repo(tmp_path, "provnolabs"))
    with pytest.raises(bs.BootstrapError) as exc:
        bs.bootstrap()
    _assert_names_the_missing_declaration(str(exc.value), "provnolabs")
    # And the refusal is not a one-shot: a second call must not find a cached
    # success left behind by the raising one.
    with pytest.raises(bs.BootstrapError):
        bs.bootstrap()


def test_provider_repo_with_lab_patterns_bootstraps(tmp_path, monkeypatch):
    """The declared case is the whole point — and the fixture really does register."""
    repo = _write_provider_repo(
        tmp_path, "provlabs", project='\n[project]\nlab_patterns = [".*"]\n'
    )
    monkeypatch.setenv("OTTO_SUT_DIRS", repo)
    result = bs.bootstrap()
    assert result.errors == []
    assert [r.name for r in result.repos] == ["provlabs"]
    # Without this the negative tests would pass just as well against a fixture
    # whose init module registered nothing at all.
    from otto.host.product import registered_product_providers

    assert "provlabs" in {owner for _, owner in registered_product_providers()}


def test_provider_owner_check_reads_the_subscription():
    """D2 reads provider owners from the subscription, judged over the repos that ran."""
    from types import SimpleNamespace

    from otto.host.product import register_product_provider
    from otto.registry import registering_repo

    ghost = SimpleNamespace(
        name="ghost",
        sut_dir="/nowhere/ghost",
        project_scope=None,
        declared_products=[],
        declared_dev_tools=[],
    )
    with registering_repo("ghost"):
        register_product_provider(lambda host: [])
    # The positive control: once ghost is among the repos that ran, its
    # provider makes it owe a [project] declaration it does not have.
    with pytest.raises(bs.ProjectScopeError, match="ghost"):
        bs._check_providing_repos_declare_scope([ghost], [ghost])
    # Absent from ordered_repos, its provider's owner is ignored.
    bs._check_providing_repos_declare_scope([ghost], [])


def test_providerless_repo_needs_no_declaration(tmp_path, monkeypatch):
    """A repo that registers no provider owes no ``[project]`` block."""
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo(tmp_path))
    assert bs.bootstrap().errors == []


# An explicitly empty declaration is the SAME defect as a missing one: both
# compile to a scope that fullmatches no lab, so the providers are dead code.
EMPTY_LAB_SCOPES = [
    ("emptylablist", "\n[project]\nlab_patterns = []\n"),
    ("nolabkey", '\n[project]\nhost_patterns = [".*"]\n'),
]


@pytest.mark.parametrize(
    ("stem", "project"), EMPTY_LAB_SCOPES, ids=[stem for stem, _ in EMPTY_LAB_SCOPES]
)
def test_explicitly_empty_lab_scope_fails_like_a_missing_one(tmp_path, monkeypatch, stem, project):
    """``lab_patterns = []`` (and a ``[project]`` without the key) must refuse too."""
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_provider_repo(tmp_path, stem, project=project))
    with pytest.raises(bs.BootstrapError) as exc:
        bs.bootstrap()
    _assert_names_the_missing_declaration(str(exc.value), stem)


def test_provider_repo_with_empty_host_patterns_fails(tmp_path, monkeypatch):
    """``host_patterns = []`` admits no host — distinct from the ``[".*"]`` default."""
    project = '\n[project]\nlab_patterns = [".*"]\nhost_patterns = []\n'
    monkeypatch.setenv(
        "OTTO_SUT_DIRS", _write_provider_repo(tmp_path, "emptyhosts", project=project)
    )
    with pytest.raises(bs.BootstrapError) as exc:
        bs.bootstrap()
    msg = str(exc.value)
    assert "emptyhosts" in msg
    assert "host_patterns" in msg  # the axis at fault, not the lab one


def test_dev_tool_provider_repo_needs_lab_patterns_too(tmp_path, monkeypatch):
    """The dev-tool registry is a SEPARATE list — a check reading only products passes above."""
    monkeypatch.setenv(
        "OTTO_SUT_DIRS", _write_provider_repo(tmp_path, "devtoolrepo", seam="dev_tool")
    )
    with pytest.raises(bs.BootstrapError) as exc:
        bs.bootstrap()
    _assert_names_the_missing_declaration(str(exc.value), "devtoolrepo")


def _write_declaring_repo(tmp_path, stem: str, *, project: str = "", seam: str = "products") -> str:
    """A repo with one ``[[products]]``/``[[dev_tools]]`` entry, plus *project* TOML.

    Unlike :func:`_write_provider_repo`, no init module is needed:
    ``declared_products``/``declared_dev_tools`` are populated straight from
    ``Repo.parse_settings`` (settings parse alone), so a bare TOML array is
    enough to trip D2 on the declarative seam.
    """
    entry = f'[[{seam}]]\nname = "{stem}-entry"\nkind = "noop"\n'
    repo = make_sut_repo(tmp_path / stem, name=stem, extra=entry + textwrap.dedent(project))
    return str(repo)


def test_declaring_repo_without_lab_patterns_fails_bootstrap(tmp_path, monkeypatch):
    """D2 parity: a repo with ``[[products]]`` entries but no ``[project]`` must fail too."""
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_declaring_repo(tmp_path, "declnolabs"))
    with pytest.raises(bs.BootstrapError) as exc:
        bs.bootstrap()
    _assert_names_the_missing_declaration(str(exc.value), "declnolabs")
    with pytest.raises(bs.BootstrapError):
        bs.bootstrap()


def test_declaring_repo_with_lab_patterns_bootstraps(tmp_path, monkeypatch):
    """The declared case, WITH ``[project]``, must bootstrap clean."""
    repo = _write_declaring_repo(
        tmp_path, "decllabs", project='\n[project]\nlab_patterns = [".*"]\n'
    )
    monkeypatch.setenv("OTTO_SUT_DIRS", repo)
    result = bs.bootstrap()
    assert result.errors == []
    assert [r.name for r in result.repos] == ["decllabs"]


def test_dev_tools_declaring_repo_without_lab_patterns_fails_bootstrap(tmp_path, monkeypatch):
    """The dev_tools array is a separate list from products — cover it too."""
    monkeypatch.setenv(
        "OTTO_SUT_DIRS", _write_declaring_repo(tmp_path, "devtdeclnolabs", seam="dev_tools")
    )
    with pytest.raises(bs.BootstrapError) as exc:
        bs.bootstrap()
    _assert_names_the_missing_declaration(str(exc.value), "devtdeclnolabs")


def test_dependency_skipped_declaring_repo_is_excused_and_contributes_nothing(
    tmp_path, monkeypatch
):
    """A dependency-skipped repo's declared entries neither gate nor apply.

    Its `[[products]]` array was parsed in phase 1, but `declared_for_host`
    filters skipped repos out at collection (Chris, 2026-09-02) — entries that
    can never apply owe no `[project]` scope, and the missing dependency has
    already surfaced as its own finding; refusing bootstrap over scope on top
    would bury the real problem. Both directions pinned: bootstrap does not
    raise, and the skipped repo's entries do not collect.
    """
    from otto.declared import declared_for_host

    entry = '[[products]]\nname = "declskipped-entry"\nkind = "noop"\n'
    repo = make_sut_repo(
        tmp_path / "declskipped",
        name="declskipped",
        extra=entry + '[dependencies]\nrequired = ["ghost"]\n',
    )
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
    result = bs.bootstrap()
    assert any("ghost" in str(e) for e in result.errors)  # the real finding
    assert [r.name for r in result.ordered_repos] == []  # the repo was skipped
    host = types.SimpleNamespace(id="h1", source_lab="")
    assert declared_for_host(host, "declared_products") == []


def test_providerless_repo_may_declare_an_empty_scope(tmp_path, monkeypatch):
    """The check is gated on what a repo REGISTERED, never on the shape of its config.

    A repo with no providers is free to scope itself to nothing — that is a repo
    saying "none of these labs are mine", which costs nobody anything.
    """
    repo = make_sut_repo(
        tmp_path / "quiet",
        name="quiet",
        extra="[project]\nlab_patterns = []\nhost_patterns = []\n",
    )
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
    assert bs.bootstrap().errors == []


# ── reentrance: the import phase can land back in bootstrap() ─────────────


def _reentrant_repo(tmp_path, body: str) -> str:
    """A SUT repo whose init module runs *body* while bootstrap imports it."""
    return str(
        make_sut_repo(
            tmp_path / "repo",
            name="repo",
            extra='init = ["reenter"]\nlibs = ["pylib"]\n',
            files={"pylib/reenter.py": textwrap.dedent(body)},
        )
    )


def test_an_init_module_that_reenters_gets_the_settled_repos(tmp_path, monkeypatch):
    """A repo init calling ``get_repos()`` is answered, not recursed.

    The import phase runs USER code, and user code reaching
    ``otto.bootstrap.get_repos()`` re-enters ``bootstrap()`` with ``_result``
    still unset — directly, or by way of a stamped host whose product
    providers consult ``scope_for_repo``. Discovery and the dependency pass
    are already done by then, so the honest answer is the repo list the outer
    call is about to publish.
    """
    monkeypatch.setenv(
        "OTTO_SUT_DIRS",
        _reentrant_repo(
            tmp_path,
            """
            from otto.bootstrap import get_repos

            SEEN = [r.name for r in get_repos()]
            """,
        ),
    )

    result = bs.bootstrap()

    import reenter  # the init module, imported by the bootstrap above

    assert reenter.SEEN == ["repo"]  # the settled list, not an empty degraded one
    assert [r.name for r in result.repos] == ["repo"]


def test_a_host_built_mid_bootstrap_gets_its_declared_entries(tmp_path, monkeypatch):
    """End-to-end pin of the non-forcing probe's `_in_progress` half.

    An init module that builds a host runs INSIDE bootstrap's phase-2 import
    window (`_in_progress` set, `_result` still None). The factory chokepoint
    applies declared entries there via `declared_for_host`, whose probe must
    answer "bootstrapped" mid-window — a probe that only counted `_result`
    would silently drop declared entries for exactly these hosts while code
    providers still applied. This is the one test that exercises the consumer
    from inside the initialization window rather than around it.
    """
    body = """
    from otto.host.element import Element
    from otto.host.factory import create_host_from_dict

    _host = create_host_from_dict(
        {
            "os_type": "unix",
            "ip": "10.0.0.9",
            "creds": [{"login": "admin", "password": "admin"}],
        },
        lab_name="bench",
        element=Element("probe-box"),
    )
    PRODUCTS = [(p.name, p.owner) for p in _host.products]
    DEV_TOOLS = [(t.name, t.owner) for t in _host.dev_tools]
    """
    settings = textwrap.dedent("""\
        init = ["declmid_init"]
        libs = ["pylib"]

        [project]
        lab_patterns = [".*"]

        [[products]]
        name = "fw"
        kind = "shell"
        artifact = "build/fw.bin"

        [[dev_tools]]
        name = "probe"
        kind = "shell"
        artifact = "tools/probe.sh"
        """)
    repo = make_sut_repo(
        tmp_path / "declmid",
        name="declmid",
        extra=settings,
        files={"pylib/declmid_init.py": textwrap.dedent(body)},
    )
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))

    result = bs.bootstrap()

    assert result.errors == []
    import declmid_init  # the init module, imported by the bootstrap above

    assert declmid_init.PRODUCTS == [("fw", "declmid")]
    assert declmid_init.DEV_TOOLS == [("probe", "declmid")]


def test_reentrance_does_not_compose_the_root_twice(tmp_path, monkeypatch):
    """The re-entrant call must NOT run a second, nested composition root.

    Nesting is not merely wasteful: the inner pass publishes ``_result`` from a
    world whose init modules are still half-imported, and every consumer that
    asks between then and the outer call's own assignment reads that half-built
    answer. Counting the dependency pass is the discriminator — a nested root
    resolves dependencies again, an answered one does not.
    """
    from otto.config import dependencies as deps_mod

    calls = []
    real = deps_mod.resolve_dependencies

    def _counting(repos):
        calls.append([r.name for r in repos])
        return real(repos)

    monkeypatch.setattr(deps_mod, "resolve_dependencies", _counting)
    monkeypatch.setenv(
        "OTTO_SUT_DIRS",
        _reentrant_repo(
            tmp_path,
            """
            from otto.bootstrap import get_repos

            SEEN = [r.name for r in get_repos()]
            """,
        ),
    )

    bs.bootstrap()

    assert calls == [["repo"]]  # exactly once — the re-entrant call was answered


def test_a_raising_bootstrap_leaves_no_partial_view_behind(tmp_path, monkeypatch):
    """A bootstrap that dies must not leave its in-progress view standing.

    D2's refusal and any uncontainable init failure raise straight out of the
    import phase. If the partial survived, the NEXT ``bootstrap()`` would read
    a half-built answer and report success for a run that never finished.
    """
    monkeypatch.setenv(
        "OTTO_SUT_DIRS",
        _reentrant_repo(
            tmp_path,
            """
            raise KeyboardInterrupt("uncontainable: not a repo bug")
            """,
        ),
    )

    with pytest.raises(KeyboardInterrupt):
        bs.bootstrap()

    assert bs._in_progress is None
    assert bs._result is None  # nothing was published


# ── is_bootstrapped(): mid-bootstrap counts as bootstrapped ───────────────


def test_is_bootstrapped_true_mid_bootstrap(monkeypatch):
    """The phase-2 re-entrant window (`_in_progress` set, `_result` not yet)
    must read as bootstrapped: `get_repos()` already answers correctly and
    for free there (the re-entrant branch returns `_in_progress`, whose
    `repos`/`ordered_repos` are final by then), so a host built by an init
    module mid-bootstrap must not have its declared entries silently dropped
    while its providers still apply.
    """
    monkeypatch.setattr(bs, "_result", None)
    monkeypatch.setattr(bs, "_in_progress", bs.BootstrapResult(env=None, repos=[]))
    assert bs.is_bootstrapped() is True


def test_is_bootstrapped_false_before_any_call(monkeypatch):
    """Only a process that has not started bootstrap at all collects nothing."""
    monkeypatch.setattr(bs, "_result", None)
    monkeypatch.setattr(bs, "_in_progress", None)
    assert bs.is_bootstrapped() is False


# ── [os_profiles]: a bad table is contained per repo, after init ──────────


def _profile_repo(tmp_path, name: str, table: str = "", *, required: str = "") -> str:
    deps = f'[dependencies]\nrequired = ["{required}"]\n\n' if required else ""
    return str(make_sut_repo(tmp_path / name, name=name, extra=deps + table))


_BAD_TABLE = '[os_profiles.broken]\nbase = "windows"\n'


def test_a_bad_os_profiles_table_is_contained_naming_the_repo_and_the_table(tmp_path, monkeypatch):
    bad = _profile_repo(tmp_path, "repo-a", _BAD_TABLE)
    good = _profile_repo(tmp_path, "repo-b", '[os_profiles.fine]\nbase = "unix"\n')
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{bad},{good}")
    result = bs.bootstrap()  # does not raise
    assert [repo.name for repo in result.repos] == ["repo-a", "repo-b"]  # still discovered
    # Kept in the dependency order, as a repo whose init failed is.
    assert [repo.name for repo in result.ordered_repos] == ["repo-a", "repo-b"]
    (err,) = result.errors
    assert err.sut_dir == pathlib.Path(bad)
    assert "[os_profiles.broken] in repo 'repo-a'" in str(err)
    assert "register_os_profile" not in str(err)


def test_a_dependency_skipped_repo_s_malformed_table_is_still_reported(tmp_path, monkeypatch):
    """A table's shape is the settings parse's to check, skipped repo or not."""
    skipped = _profile_repo(
        tmp_path, "repo-a", "[os_profiles.broken]\nbase = 5\n", required="ghost"
    )
    monkeypatch.setenv("OTTO_SUT_DIRS", skipped)
    result = bs.bootstrap()
    assert result.ordered_repos == []
    (err,) = result.errors
    assert err.sut_dir == pathlib.Path(skipped)
    assert "os_profiles.broken.base" in str(err)


def test_a_dependency_skipped_repo_s_tables_are_not_judged_by_meaning(tmp_path, monkeypatch):
    """Its init never ran, so a base it may register is not its error: the skip is its only one."""
    skipped = _profile_repo(tmp_path, "repo-a", _BAD_TABLE, required="ghost")
    monkeypatch.setenv("OTTO_SUT_DIRS", skipped)
    result = bs.bootstrap()
    assert result.ordered_repos == []
    (err,) = result.errors
    assert "dependency 'ghost' is not satisfied" in str(err)


def test_a_command_not_involving_the_broken_repo_still_runs(tmp_path):
    """The real CLI: root help with a bad table in one of two repos exits 0."""
    import os
    import subprocess
    import sys

    bad = _profile_repo(tmp_path, "repo-a", _BAD_TABLE)
    good = _profile_repo(tmp_path, "repo-b")
    env = {
        **os.environ,
        "OTTO_SUT_DIRS": f"{bad},{good}",
        "OTTO_HOME": str(tmp_path / "home"),
        "OTTO_XDIR": str(tmp_path / "xdir"),
    }
    done = subprocess.run(
        [sys.executable, "-m", "otto", "--help"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]


def _scoped(lab: str) -> str:
    return f'[project]\nlab_patterns = ["{lab}"]\nhost_patterns = [".*"]\n\n'


def test_a_bad_table_does_not_excuse_an_unscoped_provider_from_d2(tmp_path, monkeypatch):
    """D2 still judges a repo whose table is bad: its init ran and registered a provider."""
    repo = _write_provider_repo(tmp_path, "provbadtable", project=_BAD_TABLE)
    monkeypatch.setenv("OTTO_SUT_DIRS", repo)
    with pytest.raises(bs.ProjectScopeError) as exc:
        bs.bootstrap()
    _assert_names_the_missing_declaration(str(exc.value), "provbadtable")


def _dependent_pair(root, broken_repo: str, monkeypatch) -> "types.SimpleNamespace":
    """Bootstrap *broken_repo* (named repo-a) and repo-c, which requires it."""
    bs._reset()
    c = _profile_repo(root, "repo-c", required="repo-a")
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{broken_repo},{c}")
    result = bs.bootstrap()
    return types.SimpleNamespace(
        ordered=[repo.name for repo in result.ordered_repos],
        failing=sorted(pathlib.Path(e.sut_dir).name for e in result.errors),
    )


def test_a_dependent_of_a_bad_table_repo_behaves_as_with_a_failed_init(tmp_path, monkeypatch):
    """A bad table is the dependency's load error, exactly as a failed init module is."""
    table_root, init_root = tmp_path / "table", tmp_path / "init"
    bad_table = _dependent_pair(
        table_root, _profile_repo(table_root, "repo-a", _BAD_TABLE), monkeypatch
    )
    failed_init = _dependent_pair(
        init_root,
        str(
            make_sut_repo(
                init_root / "repo-a",
                name="repo-a",
                extra='libs = ["lib"]\ninit = ["repo_a_fails"]\n',
                files={"lib/repo_a_fails.py": "raise RuntimeError('init failed')\n"},
            )
        ),
        monkeypatch,
    )
    assert failed_init.ordered == ["repo-a", "repo-c"]
    assert failed_init.failing == ["repo-a"]
    assert bad_table == failed_init


def _scoped_pair(tmp_path, monkeypatch):
    bad = _profile_repo(tmp_path, "repo-a", _scoped("lab-a") + _BAD_TABLE)
    good = _profile_repo(tmp_path, "repo-b", _scoped(".*"))
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{bad},{good}")
    return bs.bootstrap()


def test_a_run_not_using_the_bad_table_repo_proceeds_with_a_warning(tmp_path, monkeypatch):
    from otto.session.projects import check_repos, select_projects

    result = _scoped_pair(tmp_path, monkeypatch)
    # Kept ordered, as a repo whose init failed is: D2 and the dependency
    # order still see it.
    assert [repo.name for repo in result.ordered_repos] == ["repo-a", "repo-b"]
    check = check_repos(result, ["lab-b"], select_projects(result.repos, [], []))
    (demoted,) = check.demoted
    assert demoted.repo == "repo-a"
    assert demoted.reason == "out_of_scope"
    assert "[os_profiles.broken] in repo 'repo-a'" in str(demoted.error)


def test_a_run_using_the_bad_table_repo_fails_loudly(tmp_path, monkeypatch):
    from otto.session.projects import RepoLoadError, check_repos, select_projects

    result = _scoped_pair(tmp_path, monkeypatch)
    with pytest.raises(RepoLoadError) as exc:
        check_repos(result, ["lab-a"], select_projects(result.repos, [], []))
    assert "[os_profiles.broken] in repo 'repo-a'" in str(exc.value)


# ── [os_profiles] over a host class registered by reference ─────────────────


def _ref_class_repo(tmp_path, name: str, init_body: str, table: str = "") -> str:
    """A repo whose init module ``<name>_init`` runs *init_body*, with settings *table*."""
    module = f"{name.replace('-', '_')}_init"
    return str(
        make_sut_repo(
            tmp_path / name,
            name=name,
            extra=f'libs = ["lib"]\ninit = ["{module}"]\n\n{table}',
            files={f"lib/{module}.py": textwrap.dedent(init_body)},
        )
    )


_MISSING_MODULE_CLASS = """\
    from otto.host.os_profile import register_host_class
    from otto.registry import Ref

    register_host_class(
        "plugin-host",
        Ref("plugin_hosts_missing:PluginHost"),
        spec=Ref("plugin_hosts_missing:PluginSpec"),
    )
"""

_ALPHA_OVER_PLUGIN_HOST = '[os_profiles.alpha]\nbase = "plugin-host"\n'


def test_a_table_over_a_class_whose_module_is_missing_is_contained(tmp_path, monkeypatch):
    """The class's import fails at the table check: that repo's error, never a crash."""
    repo = _ref_class_repo(tmp_path, "repo-ref", _MISSING_MODULE_CLASS, _ALPHA_OVER_PLUGIN_HOST)
    monkeypatch.setenv("OTTO_SUT_DIRS", repo)
    result = bs.bootstrap()  # does not raise
    (err,) = result.errors
    assert err.sut_dir == pathlib.Path(repo)
    text = str(err)
    assert "[os_profiles.alpha] in repo 'repo-ref'" in text
    assert "base 'plugin-host' (host class registered by repo_ref_init)" in text
    assert "resolution failed: ModuleNotFoundError" in text
    assert "plugin_hosts_missing" in text


def test_a_ref_class_s_bad_own_profile_names_its_registering_module(tmp_path, monkeypatch):
    """The class's own defaults are checked when it resolves; the error names who registered it.

    The table that triggered the resolution belongs to another repo, so naming
    only that table would send its author after a typo that is not theirs.
    """
    owner = _ref_class_repo(
        tmp_path,
        "repo-owner",
        """\
        from otto.host.os_profile import ProfileFields, register_host_class
        from otto.registry import FrozenMap, Ref

        register_host_class(
            "typo-host",
            Ref("otto.host.unix_host:UnixHost"),
            spec=Ref("otto.models.host:UnixHostSpec"),
            profile=ProfileFields(FrozenMap({"not_a_field": 1})),
        )
        """,
    )
    user = _profile_repo(tmp_path, "repo-user", '[os_profiles.alpha]\nbase = "typo-host"\n')
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{owner},{user}")
    result = bs.bootstrap()
    (err,) = result.errors
    text = str(err)
    assert "[os_profiles.alpha] in repo 'repo-user'" in text
    assert "base 'typo-host' (host class registered by repo_owner_init)" in text
    assert "unknown default field(s)" in text
