"""The composed state lives in ``otto.declared``; ``otto.bootstrap`` re-exports its readers.

Spec 2026-10-11 placement-and-phases §3.9: the complete and in-progress results,
the initiator and every reader over them have one home, below the root. The
root re-exports the same objects, so a user's ``otto.bootstrap.get_repos`` is
the function the layers below the root call. These pin that, and the three
lifecycle rows the move keeps: an init module that reads the state while
composition runs gets the result being built, a composition that fails
publishes nothing and the next read composes again, and invalidation withdraws
what was published and leaves the initiator in place.
"""

import textwrap

import pytest

from otto import bootstrap, declared
from tests._fixtures.sutrepo import make_sut_repo


@pytest.fixture(autouse=True)
def _nothing_composed():
    bootstrap.invalidate()
    yield
    bootstrap.invalidate()


def _repo(tmp_path, body: str, later: "str | None" = None) -> str:
    """A SUT repo whose init module ``composed_state_probe`` runs *body*.

    With *later*, a second init module, ``composed_state_later``, runs it next.
    """
    inits = '"composed_state_probe", "composed_state_later"' if later else '"composed_state_probe"'
    files = {"pylib/composed_state_probe.py": textwrap.dedent(body)}
    if later:
        files["pylib/composed_state_later.py"] = textwrap.dedent(later)
    return str(
        make_sut_repo(
            tmp_path / "repo",
            name="repo",
            extra=f'init = [{inits}]\nlibs = ["pylib"]\n',
            files=files,
        )
    )


def _counted_passes(monkeypatch) -> "list[list[str]]":
    """Count compositions: each runs the dependency pass exactly once."""
    from otto.config import dependencies as deps_mod

    calls: "list[list[str]]" = []
    real = deps_mod.resolve_dependencies

    def _counting(repos):
        calls.append([r.name for r in repos])
        return real(repos)

    monkeypatch.setattr(deps_mod, "resolve_dependencies", _counting)
    return calls


@pytest.mark.parametrize(
    "name", ["BootstrapResult", "get_repos", "get_ordered_repos", "is_bootstrapped"]
)
def test_the_root_re_exports_the_catalogs_own_object(name):
    assert name in bootstrap.__all__
    assert getattr(bootstrap, name) is getattr(declared, name)


def test_the_root_keeps_no_result_and_no_discovery_of_its_own():
    assert not hasattr(bootstrap, "_result")
    assert not hasattr(bootstrap, "_in_progress")
    assert not hasattr(bootstrap, "_discovered")


def test_an_init_module_reading_the_state_mid_composition_gets_the_result_being_built(
    tmp_path, monkeypatch
):
    body = """
        from otto.declared import (
            compose,
            current_ordered_repos,
            current_repos,
            get_ordered_repos,
            get_repos,
            is_bootstrapped,
        )

        SEEN = [
            [r.name for r in get_repos()],
            [r.name for r in current_repos()],
            [r.name for r in get_ordered_repos()],
            [r.name for r in current_ordered_repos()],
            is_bootstrapped(),
        ]
        BUILDING = compose()
        ERRORS_THEN = len(BUILDING.errors)
        """
    later = 'raise ValueError("a later init fails, contained")\n'
    calls = _counted_passes(monkeypatch)
    monkeypatch.setenv("OTTO_SUT_DIRS", _repo(tmp_path, body, later))
    result = bootstrap.bootstrap()

    import composed_state_probe

    assert composed_state_probe.SEEN == [["repo"], ["repo"], ["repo"], ["repo"], True]
    # The result being built, never a second, nested composition: a nested one
    # would carry an errors list of its own (discovery's repos list is shared),
    # and run a second dependency pass.
    assert composed_state_probe.BUILDING.errors is result.errors
    assert composed_state_probe.ERRORS_THEN == 0
    assert any(
        "a later init fails, contained" in str(e) for e in composed_state_probe.BUILDING.errors
    )
    assert calls == [["repo"]]
    assert declared.compose() is result


def test_a_failed_composition_publishes_nothing_and_the_next_read_composes_again(
    tmp_path, monkeypatch
):
    calls = _counted_passes(monkeypatch)
    monkeypatch.setenv(
        "OTTO_SUT_DIRS", _repo(tmp_path, 'raise KeyboardInterrupt("uncontainable")\n')
    )
    with pytest.raises(KeyboardInterrupt) as first:
        declared.get_repos()
    assert declared.published_result() is None
    assert declared.is_bootstrapped() is False

    # The init is never repaired: the next read composes again and fails the same way.
    with pytest.raises(KeyboardInterrupt) as second:
        declared.compose()
    assert declared.published_result() is None
    assert declared.is_bootstrapped() is False
    assert [type(first.value), str(first.value)] == [type(second.value), str(second.value)]
    assert calls == [["repo"], ["repo"]]


def test_invalidation_withdraws_what_was_published_and_keeps_the_initiator(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", _repo(tmp_path, "X = 1\n"))
    first = declared.compose()
    assert declared.published_result() is first

    bootstrap.invalidate()
    assert declared.published_result() is None
    assert declared.is_bootstrapped() is False

    second = declared.compose()
    assert second is not first
    assert [r.name for r in second.repos] == ["repo"]
