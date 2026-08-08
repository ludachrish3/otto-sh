"""bootstrap(): phases, idempotence, containment framing."""

import pathlib
import textwrap

import pytest

from otto import bootstrap as bs
from tests._fixtures.sutrepo import make_sut_repo


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
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


def test_broken_test_file_is_contained_and_framed(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo(tmp_path, broken_test=True))
    result = bs.bootstrap()
    assert len(result.errors) == 1
    msg = str(result.errors[0])
    assert "failed to load" in msg
    assert "test_broken.py" in msg
    assert "repo" in msg
    assert isinstance(result.errors[0].__cause__, SyntaxError)


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


def _write_repo_with_test_body(tmp_path, stem: str, body: str) -> str:
    """A repo whose one top-level test file is named ``test_<stem>.py`` and runs *body*.

    *stem* must be unique per case. ``Repo.import_test_file`` keys ``sys.modules``
    on the file STEM alone and early-returns when the name is already present, so
    parametrized cases sharing a filename would silently skip the import after the
    first and pass vacuously — a guard that cannot fail.
    """
    repo = make_sut_repo(
        tmp_path / stem,
        name=stem,
        tests=["tests"],
        files={f"tests/test_{stem}.py": textwrap.dedent(body)},
    )
    return str(repo)


# The two documented ways a pytest module declines to load, plus the assertion
# flavour. All three raise BaseException subclasses that are NOT Exception, so a
# containment seam filtering on `except Exception` lets them brick every command.
DECLINING_MODULE_BODIES = [
    ("importorskip", "import pytest\npytest.importorskip('otto_no_such_optional_dep')\n"),
    ("skipmodule", "import pytest\npytest.skip('needs a dep', allow_module_level=True)\n"),
    ("failnotrace", "import pytest\npytest.fail('cannot load', pytrace=False)\n"),
]


@pytest.mark.parametrize(("stem", "body"), DECLINING_MODULE_BODIES)
def test_module_level_pytest_outcome_is_contained(tmp_path, monkeypatch, stem, body):
    """A test module that DECLINES to load must not traceback out of every command.

    ``pytest.importorskip`` is the mainstream idiom for an optional dependency and
    bootstrap execs every top-level ``test_*.py`` on EVERY otto command, so before
    this was contained a single such file made ``otto schema export`` dump a raw
    traceback and exit 1.
    """
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo_with_test_body(tmp_path, stem, body))
    try:
        result = bs.bootstrap()
    except BaseException as exc:  # the escape IS the defect under test
        # Deliberately NOT a bare call: an escaping `Skipped` reaches pytest's own
        # outcome machinery and marks THIS TEST skipped rather than failed, so the
        # regression would hide behind a green-looking summary. Convert it to an
        # AssertionError (a plain Exception) so the guard can only ever fail loudly.
        raise AssertionError(
            f"bootstrap() let {type(exc).__name__} escape the containment seam: {exc!r}"
        ) from exc
    assert len(result.errors) == 1
    msg = str(result.errors[0])
    assert "failed to load" in msg
    assert f"test_{stem}.py" in msg


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
    Ctrl-C during bootstrap would see their interrupt turned into a framed
    'failed to load' line and otto would carry on.
    """
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo_with_test_body(tmp_path, stem, body))
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
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo_with_test_body(tmp_path, "cancelled", body))
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
    """The init-module seam needs its own guard — it is not the test-file seam.

    ``settings.toml``'s ``init`` list names user modules that register hosts,
    products and ``@instruction`` commands, and bootstrap imports them through a
    SEPARATE ``try``. An earlier revision of this fix widened all three seams but
    gated only the test-file one, so reverting this one to ``except Exception``
    left the suite green.
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
