"""A fleet walk never widens silently (spec 4 §4; its §8 walk-contract table).

Real SUT trees under ``OTTO_SUT_DIRS`` and a real bootstrap, on hand-built
contexts that never ran ``check_repos``: the case the CLI's own gate does not
cover. Each test names its row.
"""

import logging
import os
from pathlib import Path

import pytest

from otto import bootstrap as bs
from otto.bootstrap import BootstrapError, DependencyError, ProjectScopeError
from otto.config.scope import resolve_scopes
from otto.context import LIBRARY_LAB_NAME, OttoContext, set_context
from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID
from otto.session.errors import RepoLoadError
from tests._fixtures.bootstrap_seam import patch_bootstrap
from tests._fixtures.fleet import _lab
from tests._fixtures.sutrepo import make_sut_repo


@pytest.fixture(autouse=True)
def _fresh_bootstrap():
    bs.invalidate()
    yield
    bs.invalidate()


def _write(tmp_path, name, *, labs=None, hosts="slot1", required=(), init_fails=False, extra=""):
    """A real repo. *labs* None = no [project]; *init_fails* adds an init module that raises."""
    top, files = "", {}
    if init_fails:
        module = f"{name.replace('-', '_')}_init"
        top = f'libs = ["."]\ninit = ["{module}"]\n\n'
        files[f"{module}.py"] = "raise RuntimeError('init boom')\n"
    body = top + extra
    if labs is not None:
        body += f'\n[project]\nlab_patterns = ["{labs}"]\nhost_patterns = ["{hosts}"]\n'
    if required:
        body += "\n[dependencies]\nrequired = [" + ", ".join(f'"{r}"' for r in required) + "]\n"
    return make_sut_repo(tmp_path / name, name=name, extra=body, files=files)


def _unparseable(tmp_path, name="broken"):
    return make_sut_repo(tmp_path / name, name=name, extra="this is = = not toml")


def _use(monkeypatch, *dirs):
    monkeypatch.setenv("OTTO_SUT_DIRS", ",".join(str(d) for d in dirs))


def _rig():
    return _lab(("slot1", "rig"), ("slot2", "rig"))


def _walk(ctx):
    return [h.id for h in ctx.all_hosts()]


def test_row1_an_unreadable_environment_raises_instead_of_walking_the_lab(tmp_path, monkeypatch):
    _use(monkeypatch, tmp_path / "does-not-exist")
    with pytest.raises(FileNotFoundError):
        _walk(OttoContext(lab=_rig()))


def test_row2_an_unparseable_settings_file_refuses(tmp_path, monkeypatch):
    broken = _unparseable(tmp_path)
    _use(monkeypatch, _write(tmp_path, "good", labs="rig"), broken)
    with pytest.raises(RepoLoadError) as raised:
        _walk(OttoContext(lab=_rig()))
    assert [Path(e.sut_dir).resolve() for e in raised.value.errors] == [broken.resolve()]


def test_row3_an_active_dependency_skipped_declarer_refuses(tmp_path, monkeypatch):
    _use(monkeypatch, _write(tmp_path, "b", labs="rig", required=["ghost"]))
    with pytest.raises(RepoLoadError) as raised:
        _walk(OttoContext(lab=_rig()))
    assert all(isinstance(e, DependencyError) for e in raised.value.errors)


def test_row4_a_switched_off_skipped_declarer_keeps_its_declaration(tmp_path, monkeypatch):
    _use(monkeypatch, _write(tmp_path, "a"), _write(tmp_path, "b", labs="rig", required=["ghost"]))
    ctx = OttoContext(lab=_rig(), exclude_projects=("b",))
    assert ctx.scopes["b"].declared
    with pytest.raises(ProjectScopeError):  # the existing empty-fleet refusal, never the lab
        _walk(ctx)


def test_row5_an_active_repo_whose_init_failed_refuses(tmp_path, monkeypatch):
    _use(monkeypatch, _write(tmp_path, "b", labs="rig", init_fails=True))
    with pytest.raises(RepoLoadError):
        _walk(OttoContext(lab=_rig()))


@pytest.mark.parametrize("off", ["excluded", "lab_inactive"])
def test_row6_an_inactive_broken_repo_is_demoted_and_its_declaration_counts(
    tmp_path, monkeypatch, off
):
    labs = "rig" if off == "excluded" else "other"
    _use(monkeypatch, _write(tmp_path, "a"), _write(tmp_path, "b", labs=labs, init_fails=True))
    exclude = ("b",) if off == "excluded" else ()
    ctx = OttoContext(lab=_rig(), exclude_projects=exclude)
    assert ctx.scopes["b"].declared  # no RepoLoadError, and the declaration is kept
    with pytest.raises(ProjectScopeError):
        _walk(ctx)


@pytest.mark.parametrize(("include", "exclude"), [((), ("My_Repo",)), (("my-repo",), ("my-repo",))])
def test_row7_selection_spelling_is_normalised_and_exclusion_wins(
    tmp_path, monkeypatch, include, exclude
):
    _use(
        monkeypatch, _write(tmp_path, "a"), _write(tmp_path, "my-repo", labs="rig", init_fails=True)
    )
    ctx = OttoContext(lab=_rig(), include_projects=include, exclude_projects=exclude)
    assert "my-repo" in ctx.scopes  # resolved, not refused


def test_row8_the_sentinel_lab_resolves_nothing(tmp_path, monkeypatch):
    _use(monkeypatch, _unparseable(tmp_path))
    lab = _rig()
    lab.name = LIBRARY_LAB_NAME
    assert OttoContext(lab=lab).scopes == {}


def test_row9_a_refusal_is_cached(tmp_path, monkeypatch):
    calls = []
    unowned = BootstrapError(tmp_path / "x", ".otto/settings.toml", ValueError("bad"))
    result = patch_bootstrap(monkeypatch, [], errors=[unowned])
    monkeypatch.setattr("otto.bootstrap.bootstrap", lambda: calls.append(1) or result)
    ctx = OttoContext(lab=_rig())
    for _ in range(2):
        with pytest.raises(RepoLoadError):
            ctx.scopes  # noqa: B018 — the read is the act under test
    assert calls == [1]


def test_row10_coverage_detection_answers_false_once_with_a_warning(tmp_path, monkeypatch, caplog):
    cov = _write(tmp_path, "cov", labs="rig", extra='[coverage]\nhosts = "slot.*"\n')
    _use(monkeypatch, cov, _unparseable(tmp_path))
    ctx = OttoContext(lab=_rig())
    with caplog.at_level(logging.WARNING, logger="otto.context"):
        assert ctx.cov is False
    assert [r.levelno for r in caplog.records if "coverage detection" in r.getMessage()] == [
        logging.WARNING
    ]


def _chain(**kw):
    from otto.cli import remote_completion as rc

    base = {"host_id": "", "hop": "", "term": None, "labs": ["rig"], "holder": "carol"}
    return rc._ChainParams(**{**base, **kw})


def test_row11_remote_completion_meets_the_refusal(tmp_path, monkeypatch):
    from otto.cli import remote_completion as rc

    _use(monkeypatch, _write(tmp_path, "good", labs="rig"), _unparseable(tmp_path))
    monkeypatch.setattr("otto.session.lab.build_lab", lambda repos, labs: _rig())
    with pytest.raises(RepoLoadError):  # remote_path_completer's catch-all turns it into []
        rc._required_for(_chain())


def test_row12_remote_completion_carries_include_projects(tmp_path, monkeypatch):
    from otto.cli import remote_completion as rc

    _use(monkeypatch, _write(tmp_path, "a"), _write(tmp_path, "b", labs="other", init_fails=True))
    monkeypatch.setattr("otto.session.lab.build_lab", lambda repos, labs: _rig())
    rc._required_for(_chain())  # lab-inactive: demoted, no refusal
    with pytest.raises(RepoLoadError):
        rc._required_for(_chain(include_projects=["b"]))


def test_row13_completion_without_a_reservation_gate_reads_no_scope(tmp_path, monkeypatch):
    from otto.cli import remote_completion as rc

    _use(monkeypatch, _write(tmp_path, "good", labs="rig"), _unparseable(tmp_path))

    def _never(chain):
        raise AssertionError("_required_for reached without a [reservations] table")

    monkeypatch.setattr("otto.cli.remote_completion._required_for", _never)
    assert rc._reservation_allows(_chain()) is True


def test_row14_the_monitors_d3_check_lets_the_refusal_through(tmp_path, monkeypatch):
    from otto.monitor.live import _enforce_driving_repo_scope

    _use(monkeypatch, _write(tmp_path, "good", labs="rig"), _unparseable(tmp_path))
    set_context(OttoContext(lab=_rig()))
    with pytest.raises(RepoLoadError):
        _enforce_driving_repo_scope()


def test_row15_a_lab_name_is_never_normalised(tmp_path, monkeypatch):
    _use(monkeypatch, _write(tmp_path, "bench", labs="Bench_A", init_fails=True))
    with pytest.raises(RepoLoadError):
        _walk(OttoContext(lab=_lab(("slot1", "Bench_A"))))


@pytest.mark.parametrize("exclude", [(), ("first",)])
def test_row16_d3_sees_a_skipped_first_repo(tmp_path, monkeypatch, exclude):
    from otto.monitor.live import _enforce_driving_repo_scope
    from otto.project.orchestrator import _enforce_current_scope

    first = _write(tmp_path, "first", labs="other", required=["ghost"])
    _use(monkeypatch, first, _write(tmp_path, "second", labs="rig"))
    ctx = OttoContext(lab=_rig(), exclude_projects=exclude)
    set_context(ctx)
    with pytest.raises(ProjectScopeError):
        _enforce_current_scope(ctx)
    with pytest.raises(ProjectScopeError):
        _enforce_driving_repo_scope()


@pytest.mark.asyncio
async def test_row17_status_full_shows_a_skipped_declarers_verdict(tmp_path, monkeypatch):
    from otto.project.orchestrator import status

    # The healthy repo is FIRST: D3 judges repos[0], and a lab-inactive first repo would refuse.
    skipped = _write(tmp_path, "skipped", labs="other", required=["ghost"])  # lab-inactive: demoted
    _use(monkeypatch, _write(tmp_path, "healthy", labs="rig"), skipped)
    set_context(OttoContext(lab=_rig()))
    report = await status()
    assert list(report.scoping) == ["healthy", "skipped"]  # get_repos() order
    assert (report.scoping["healthy"].skipped, report.scoping["skipped"].skipped) == (False, True)


def test_row18_no_environment_at_all_walks_the_whole_lab(monkeypatch):
    from otto.monitor.live import _enforce_driving_repo_scope

    for key in [k for k in os.environ if k.startswith("OTTO_")]:
        monkeypatch.delenv(key)
    ctx = OttoContext(lab=_rig())
    assert _walk(ctx) == ["slot1", "slot2"]
    set_context(ctx)
    _enforce_driving_repo_scope()  # returns quietly


def test_row19_a_healthy_tree_resolves_the_verdicts_it_always_did(tmp_path, monkeypatch):
    a = _write(tmp_path, "a", labs="rig", hosts="slot1")
    b = _write(tmp_path, "b", labs="rig", hosts="slot2", extra="")
    _use(monkeypatch, b, a)
    lab = _rig()
    expected = resolve_scopes(
        bs.bootstrap().ordered_repos,
        lab.component_names,
        lab.hosts,
        exclude_ids=frozenset({BUILTIN_LOCAL_HOST_ID}),
    )
    assert OttoContext(lab=lab).scopes == expected
