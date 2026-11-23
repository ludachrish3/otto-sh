"""Pins for the warm sequence inside ``measure_surface`` (``scripts/import_budget.py``).

seed -> ``_prove_port_closed`` -> post-seed writer -> backdate -> snapshot ->
measured run -> checks, all in the one sequence. ``measure`` is stubbed: no
strace runs, and the stub plays the seed and the measured run against a real
home in ``tmp_path``. The post-seed writers run for real, as children of the
measured interpreter, since what they print is the contract under test.
"""

import difflib
import json
import os
import time
from pathlib import Path
from typing import ClassVar

import pytest

from otto import _shim_complete
from tests._fixtures.budget_harness import load_harness

harness = load_harness()

COMPLETION = (("_OTTO_COMPLETE", "complete_bash"), ("COMP_WORDS", "otto "), ("COMP_CWORD", "1"))


def _surface(**fields) -> object:
    return harness.Surface(
        "completion_x", ["otto"], real_entry=True, warm=True, env_extra=COMPLETION, **fields
    )


_PATHS: "dict[tuple[str, str], object]" = {}


def _paths(env: dict[str, str]):
    """The cache and sidecar otto resolves in *env* (``harness.warm_paths``), once per home."""
    key = (env["OTTOPATH"], env["OTTO_HOME"])
    if key not in _PATHS:
        _PATHS[key] = harness.warm_paths(env)
    return _PATHS[key]


def _cache(env: dict[str, str]) -> Path:
    return Path(_paths(env).cache)


_MARKER_PATH = _shim_complete.marker_path
"""Held here, so a test may take ``marker_path`` off the shim and still find the marker."""


def _marker(env: dict[str, str], which: str = "names") -> Path:
    return Path(_MARKER_PATH(str(_cache(env).parent), which))


def _measured_result() -> dict:
    ops = {"total": 7, "workspace": 1, "by_bucket": {}, "by_process": {}, "by_path": {}}
    return {"exit_code": 0, "exception": None, "modules": [], "stdout": "", "file_ops": ops}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A surface env under *tmp_path* and a ``measure`` stub that records each call's env.

    The seed (the first call) writes the cache and the ``names`` marker, as a
    real seed TAB does; the measured run does whatever ``world.on_measured``
    says, which by default is the stat leg's marker touch.
    """
    project = tmp_path / "project"
    project.mkdir()

    def surface_env(surface):
        env = harness._sanitized_env()
        env.update({"OTTOPATH": str(project), "OTTO_HOME": str(tmp_path / "home")})
        env.update(surface.env_extra)
        return env

    class World:
        envs: ClassVar[list[dict[str, str]]] = []

        @staticmethod
        def on_seed(env):
            pass

        @staticmethod
        def on_measured(env):
            os.utime(_marker(env), None)

    def measure(argv, *, env=None, **_kw):
        World.envs.append(dict(env))
        if len(World.envs) == 1:
            cache = _cache(env)
            cache.parent.mkdir(parents=True)
            cache.write_text("{}")
            _marker(env).write_text("")
            World.on_seed(env)
        else:
            World.on_measured(env)
        return _measured_result()

    monkeypatch.setattr(harness, "_warm_bytecode", lambda: None)
    monkeypatch.setattr(harness, "surface_env", surface_env)
    monkeypatch.setattr(harness, "measure", measure)
    return World


def test_seed_env_remove_strips_only_the_seed(world):
    surface = _surface(seed_env_remove=("_OTTO_COMPLETE", "COMP_WORDS", "COMP_CWORD"))
    result = harness.measure_surface(surface)
    seed, measured = world.envs
    assert "_OTTO_COMPLETE" not in seed
    assert "COMP_WORDS" not in seed
    assert measured["_OTTO_COMPLETE"] == "complete_bash"
    assert seed["OTTO_HOME"] == measured["OTTO_HOME"], "one home for the pair"
    assert "ending" not in result


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("import json; print(json.dumps({'wrote': 0}))", "read back 0"),
        ("raise SystemExit('the base tag has no store_listing')", "no store_listing"),
        ("print('not json')", "no JSON line"),
        ("print('{not json')", "printed a malformed report"),
    ],
    ids=["wrote-nothing", "raised", "no-report", "malformed-report"],
)
def test_post_seed_writer_that_writes_nothing_is_an_ending(world, monkeypatch, body, why):
    """A writer that wrote nothing, failed or reported nothing ends the surface unmeasured."""
    monkeypatch.setitem(harness.POST_SEED_WRITERS, "x", body)
    surface = _surface(post_seed="x")
    result = harness.measure_surface(surface)
    assert why in result["ending"], result["ending"]
    assert len(world.envs) == 1, "nothing is measured after a failed writer"
    [violation] = harness.check_exit(surface, result)
    assert why in violation
    assert harness.report_row(surface, result)["ending"] == [violation]


def test_post_seed_failure_is_an_ending_not_a_crash(world, monkeypatch):
    monkeypatch.setitem(harness.POST_SEED_WRITERS, "x", "raise RuntimeError('boom')")
    result = harness.measure_surface(_surface(post_seed="x"))
    assert "RuntimeError: boom" in result["ending"], result["ending"]
    assert harness.check_exit(_surface(post_seed="x"), result)


def test_a_post_seed_writer_runs_in_the_surface_env_after_the_seed(world, monkeypatch):
    """It sees the surface's home and the seed's cache, and what it read back passes."""
    body = (
        "import json, os\n"
        "from otto import _shim_complete as sc\n"
        "print(json.dumps({'wrote': int(os.path.exists(sc.locate_cache(dict(os.environ))))}))\n"
    )
    monkeypatch.setitem(harness.POST_SEED_WRITERS, "x", body)
    result = harness.measure_surface(_surface(post_seed="x"))
    assert "ending" not in result, result.get("ending")
    assert len(world.envs) == 2


def test_the_seeded_listing_age_is_recorded_from_the_writer_to_the_runs_end(world, monkeypatch):
    """Monotonic, from the moment the post-seed writer finished to the measured run's end."""
    monkeypatch.setitem(harness.POST_SEED_WRITERS, "x", "print('{\"wrote\": 1}')")
    clock = [100.0]
    monkeypatch.setattr(harness.time, "monotonic", lambda: clock[0])

    def measured(env):
        os.utime(_marker(env), None)
        clock[0] = 107.5

    world.on_measured = staticmethod(measured)
    result = harness.measure_surface(_surface(post_seed="x"))
    assert result["seeded_age_seconds"] == 7.5


def test_a_surface_with_no_writer_records_no_seeded_age(world):
    assert "seeded_age_seconds" not in harness.measure_surface(_surface())


def _remote_verdict(age: float, *, answered: bool) -> list[str]:
    surface = harness.surface_by_key("completion_remote_path_hit")
    stdout = harness.REMOTE_HIT_ANSWER + "\n" if answered else ""
    result = {
        "exit_code": 0,
        "exception": None,
        "modules": list(surface.expect_loaded),
        "stdout": stdout,
        "seeded_age_seconds": age,
    }
    return harness.check_exit(surface, result)


def test_a_failed_remote_verdict_past_the_listing_ttl_names_the_expiry():
    from otto.config.remote_completion_cache import LISTING_TTL_SECONDS

    violations = _remote_verdict(LISTING_TTL_SECONDS + 3.0, answered=False)
    assert len(violations) == 2, violations
    assert violations[-1].endswith(
        f"the seeded listing expired before the TAB read it ({LISTING_TTL_SECONDS + 3.0:.1f} s "
        f"elapsed): the machine was overloaded, not a regression"
    )


def test_a_failed_remote_verdict_within_the_ttl_names_no_expiry():
    violations = _remote_verdict(1.0, answered=False)
    assert violations
    assert not any("expired" in v for v in violations)


def test_a_passing_remote_verdict_past_the_ttl_stays_a_pass():
    assert _remote_verdict(10_000.0, answered=True) == []


def test_only_a_remote_listing_writer_earns_the_expiry_note():
    """The observed-containers writer has a TTL of its own; the listing's says nothing about it."""
    surface = harness.surface_by_key("completion_docker_observed_warm")
    result = {"exit_code": 0, "exception": None, "modules": [], "stdout": "",
              "seeded_age_seconds": 10_000.0}  # fmt: skip
    violations = harness.check_exit(surface, result)
    assert violations
    assert not any("expired" in v for v in violations)


def test_docker_writer_with_no_projects_is_an_ending(monkeypatch, tmp_path):
    """The real ``docker_containers`` writer, given no projects, records nothing and reads back 0.

    ``record_docker_containers`` no-ops silently with no projects, so only the
    read-back stops the surface from measuring a TAB on an empty namespace.
    No ``OTTOPATH``: discovery finds no project. The seed is stubbed, since
    with no project it would leave no cache for ``world`` to locate.
    """
    runs: "list[dict[str, str]]" = []

    def surface_env(surface):
        env = {k: v for k, v in harness._sanitized_env().items() if k != "OTTOPATH"}
        env["OTTO_HOME"] = str(tmp_path / "home")
        env.update(surface.env_extra)
        return env

    def measure(argv, *, env=None, **_kw):
        runs.append(dict(env))
        return _measured_result()

    monkeypatch.setattr(harness, "_warm_bytecode", lambda: None)
    monkeypatch.setattr(harness, "surface_env", surface_env)
    monkeypatch.setattr(harness, "measure", measure)
    surface = _surface(post_seed="docker_containers")
    result = harness.measure_surface(surface)
    assert "read back 0" in result["ending"], result["ending"]
    assert len(runs) == 1, "nothing is measured after a writer that wrote nothing"
    assert "OTTOPATH" not in runs[0]
    [violation] = harness.check_exit(surface, result)
    assert "read back 0" in violation


def test_an_unknown_post_seed_writer_is_refused():
    with pytest.raises(KeyError, match="no post-seed writer named 'nope'"):
        harness._run_post_seed(_surface(post_seed="nope"), {})


def test_changed_cache_is_an_ending(world):
    def rewrite(env):
        os.utime(_marker(env), None)
        _cache(env).write_text('{"rebuilt": true}')

    world.on_measured = staticmethod(rewrite)
    surface = _surface(cache_unchanged=True)
    result = harness.measure_surface(surface)
    assert "completion_cache.json changed" in result["ending"], result["ending"]
    violations = harness.check_exit(surface, result)
    assert any("changed" in v for v in violations), violations


def _sidecar(env: dict[str, str]) -> Path:
    return Path(_paths(env).sidecar)


def test_warm_paths_are_the_ones_otto_and_the_shim_resolve(tmp_path):
    """The resolver's cache is the shim's; its sidecar is ``remote_completion_cache._path``'s."""
    project = tmp_path / "project"
    project.mkdir()
    env = {
        **harness._sanitized_env(),
        "OTTOPATH": str(project),
        "OTTO_HOME": str(tmp_path / "home"),
    }
    paths = harness.warm_paths(env)
    assert paths.cache == _shim_complete.locate_cache(env)
    assert Path(paths.sidecar).parent == Path(paths.cache).parent
    assert Path(paths.sidecar).name != Path(paths.cache).name


def test_warm_paths_refuse_a_cache_that_is_off(monkeypatch):
    off = '{"cache": null, "sidecar": null}'
    monkeypatch.setattr(harness, "_run_child", lambda *_a, **_kw: off)
    with pytest.raises(harness.WarmProofError, match="cache is off"):
        harness.warm_paths({})


def _with_binding(monkeypatch, jsonnet: bool) -> None:
    real = harness.warm_paths
    monkeypatch.setattr(
        harness,
        "warm_paths",
        lambda env: harness.WarmPaths(real(env).cache, real(env).sidecar, jsonnet=jsonnet),
    )


def _templated_surface():
    return _surface(cache_unchanged=True, expect_answer=("x",), expect_answer_templated=("t",))


def test_no_jsonnet_binding_drops_the_templated_answer_and_says_so(world, monkeypatch):
    """The templated lab cannot render, so its hosts leave the verdict; the count stays."""
    _with_binding(monkeypatch, jsonnet=False)
    surface = _templated_surface()
    result = harness.measure_surface(surface)
    assert result["proofs_skipped"] == [harness.SKIPPED_TEMPLATED_ANSWER]
    assert harness.SKIPPED_TEMPLATED_ANSWER == (
        "templated answer: no jsonnet binding on this interpreter"
    )
    assert result["file_ops"]["total"] == 7, "still measured"
    assert harness.check_exit(surface, {**result, "stdout": "x\n"}) == []
    missing = harness.check_exit(surface, {**result, "stdout": "t\n"})
    assert any("'x'" in v for v in missing), "the json lines are still required"


def test_with_a_jsonnet_binding_the_templated_answer_is_required(world, monkeypatch):
    _with_binding(monkeypatch, jsonnet=True)
    surface = _templated_surface()
    result = harness.measure_surface(surface)
    assert "proofs_skipped" not in result
    [violation] = harness.check_exit(surface, {**result, "stdout": "x\n"})
    assert "'t'" in violation
    assert harness.check_exit(surface, {**result, "stdout": "x\nt\n"}) == []


def test_warm_paths_report_whether_a_jsonnet_binding_loads(tmp_path):
    """Asked of the measured interpreter through ``otto.templating.binding.load_binding``."""
    project = tmp_path / "project"
    project.mkdir()
    env = {**harness._sanitized_env(), "OTTOPATH": str(project), "OTTO_HOME": str(tmp_path / "h")}
    code = (
        "import json\n"
        "try:\n"
        "    from otto.templating.binding import load_binding\n"
        "    load_binding()\n"
        "    ok = True\n"
        "except Exception:\n"
        "    ok = False\n"
        "print(json.dumps({'ok': ok}))\n"
    )
    assert harness.warm_paths(env).jsonnet is json.loads(harness._run_child(code, env))["ok"]


def test_only_the_host_tab_expects_a_templated_answer():
    """The one surface whose answer holds a rendered host; the past-separator set is json-only."""
    templated = {
        s.key: s.expect_answer_templated for s in harness.SURFACES if s.expect_answer_templated
    }
    assert templated == {"completion_host_warm": ("tmpl-0",)}
    assert all(s.templated_lab for s in harness.SURFACES if s.expect_answer_templated)


def test_the_proofs_watch_the_sidecar_otto_resolves(world, monkeypatch, tmp_path):
    """Wherever otto puts the sidecar, a run that writes it there is caught."""
    elsewhere = tmp_path / "elsewhere" / "sidecar.json"
    elsewhere.parent.mkdir()
    real = harness.warm_paths
    monkeypatch.setattr(
        harness,
        "warm_paths",
        lambda env: harness.WarmPaths(cache=real(env).cache, sidecar=str(elsewhere)),
    )

    def write_it(env):
        os.utime(_marker(env), None)
        elsewhere.write_text("{}")

    world.on_measured = staticmethod(write_it)
    result = harness.measure_surface(_surface(cache_unchanged=True))
    assert str(elsewhere) in result["ending"], result.get("ending")


def test_changed_remote_sidecar_is_an_ending(world):
    """A remote hit that re-listed and rewrote the sidecar measured a miss, not the hit."""

    def rewrite(env):
        os.utime(_marker(env), None)
        _sidecar(env).write_text('{"relisted": true}')

    world.on_seed = staticmethod(lambda env: _sidecar(env).write_text("{}"))
    world.on_measured = staticmethod(rewrite)
    surface = _surface(cache_unchanged=True)
    result = harness.measure_surface(surface)
    assert "remote_completion_cache.json changed" in result["ending"], result["ending"]


def test_a_remote_sidecar_the_run_created_is_an_ending(world):
    """No sidecar before the run and one after: the run listed live, which is a miss."""

    def create(env):
        os.utime(_marker(env), None)
        _sidecar(env).write_text("{}")

    world.on_measured = staticmethod(create)
    result = harness.measure_surface(_surface(cache_unchanged=True))
    assert "remote_completion_cache.json changed" in result["ending"], result["ending"]


def test_an_unchanged_cache_passes(world):
    result = harness.measure_surface(_surface(cache_unchanged=True, stat_leg=True))
    assert "ending" not in result, result.get("ending")


def test_untouched_marker_is_an_ending(world):
    world.on_measured = staticmethod(lambda env: None)
    surface = _surface(stat_leg=True)
    result = harness.measure_surface(surface)
    assert "names marker" in result["ending"], result["ending"]
    assert harness.check_exit(surface, result)


def test_the_stat_leg_backdates_names_and_keeps_tests_fresh(world):
    """The measured run finds the ``names`` marker out of its window and ``tests`` in its own."""
    seen = {}

    def seed(env):
        tests = _marker(env, "tests")
        tests.write_text("")
        old = time.time() - 2 * _shim_complete.CHECK_WINDOW_SECONDS
        os.utime(tests, (old, old))

    def measured(env):
        now = time.time()
        seen["names"] = now - _marker(env).stat().st_mtime
        seen["tests"] = now - _marker(env, "tests").stat().st_mtime
        os.utime(_marker(env), None)

    world.on_seed = staticmethod(seed)
    world.on_measured = staticmethod(measured)
    result = harness.measure_surface(_surface(stat_leg=True))
    assert "ending" not in result, result.get("ending")
    assert seen["names"] >= _shim_complete.WINDOW_SECONDS
    assert seen["tests"] < _shim_complete.CHECK_WINDOW_SECONDS


def test_the_stat_leg_creates_no_tests_marker(world):
    """A surface whose seed wrote no ``tests`` marker gets none: it never reads the tables."""
    harness.measure_surface(_surface(stat_leg=True))
    assert not _marker(world.envs[0], "tests").exists()


def test_a_seed_that_left_no_names_marker_is_proven_by_the_run_creating_it(world):
    """A cold seed TAB writes no marker: the run takes the stat leg on its absence, creating it."""
    world.on_seed = staticmethod(lambda env: _marker(env).unlink())
    world.on_measured = staticmethod(lambda env: _marker(env).write_text(""))
    result = harness.measure_surface(_surface(stat_leg=True))
    assert "ending" not in result, result.get("ending")


def test_a_run_that_never_created_the_missing_marker_is_an_ending(world):
    world.on_seed = staticmethod(lambda env: _marker(env).unlink())
    world.on_measured = staticmethod(lambda env: None)
    result = harness.measure_surface(_surface(stat_leg=True))
    assert "names marker" in result["ending"], result["ending"]


def test_a_seed_that_left_no_cache_is_an_ending(world):
    world.on_seed = staticmethod(lambda env: _cache(env).unlink())
    result = harness.measure_surface(_surface(cache_unchanged=True))
    assert "no completion_cache.json" in result["ending"], result["ending"]
    assert len(world.envs) == 1


def test_a_cache_only_surface_needs_no_marker_path(world, monkeypatch):
    """An otto from before ``marker_path`` (a base tag under the delta) still proves the cache."""
    monkeypatch.delattr(_shim_complete, "marker_path")
    result = harness.measure_surface(_surface(cache_unchanged=True))
    assert "ending" not in result, result.get("ending")
    assert "proofs_skipped" not in result
    assert len(world.envs) == 2


def test_a_stat_leg_without_marker_path_is_measured_with_the_proof_skipped(world, monkeypatch):
    """No fallback join: the stat-leg proof is skipped and named, and the run still counts."""
    monkeypatch.delattr(_shim_complete, "marker_path")
    world.on_measured = staticmethod(lambda env: None)
    surface = _surface(cache_unchanged=True, stat_leg=True)
    result = harness.measure_surface(surface)
    assert "ending" not in result, result.get("ending")
    assert result["proofs_skipped"] == [harness.SKIPPED_STAT_LEG]
    assert result["file_ops"]["total"] == 7, "the measured run's counts"
    assert harness.check_exit(surface, result) == []
    row = harness.report_row(surface, result)
    assert row["proofs_skipped"] == ["stat_leg: marker_path unavailable at base"]
    assert row["ending"] == []


def test_marker_path_is_the_shim_s_one_join(tmp_path):
    assert _shim_complete.marker_path(str(tmp_path), "names") == str(
        tmp_path / _shim_complete.MARKER_FILENAMES["names"]
    )
    with pytest.raises(KeyError):
        _shim_complete.marker_path(str(tmp_path), "other")


# --- The keys-scoped update (--only) -----------------------------------------


def _ops(total: int) -> dict:
    return {"total": total, "workspace": 2, "by_bucket": {"otto:*": total}, "by_process": {},
            "by_path": {}}  # fmt: skip


@pytest.fixture
def scoped(tmp_path, monkeypatch):
    """Two minors' ceilings files under *tmp_path*, and measurements that need no strace or uv."""
    monkeypatch.setattr(harness, "CEILINGS_DIR", tmp_path)
    for name, fresh in [
        ("_MEASURED", None),
        ("_FACTS", {}),
        ("_BASELINES", {}),
        ("_WARMED", set()),
    ]:
        monkeypatch.setattr(harness, name, fresh)
    monkeypatch.setattr(harness, "find_interpreter", lambda m: f"/usr/bin/python{m}")
    monkeypatch.setattr(harness, "build_env", lambda m, **_kw: Path(f"/budget/{m}/bin/python"))
    monkeypatch.setattr(
        harness, "measured_facts", lambda: {"minor": harness.measured_python().split("/")[2]}
    )
    keep = {"file_ops": 1, "workspace": 1, "by_bucket": {"z": 1}, "by_process": {"python": 1}}
    for minor in ("3.10", "3.11"):
        surfaces = {
            "help_project_warm": dict(keep),
            "run": dict(keep),
            "a_surface_since_removed": dict(keep),
        }
        payload = {"interpreter": minor, "surfaces": surfaces}
        (tmp_path / f"{minor}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )
    measured: "list[tuple[str, str]]" = []

    def measure_surface(s):
        minor = harness.measured_python().split("/")[2]
        measured.append((minor, s.key))
        return {"exit_code": 0, "exception": None, "file_ops": _ops(500)}

    monkeypatch.setattr(harness, "measure_surface", measure_surface)
    return measured


def _entries(path: Path) -> dict:
    """Each surface's entry in *path*, as the exact bytes of its JSON text."""
    data = json.loads(path.read_text())
    return {k: json.dumps(v, indent=2, sort_keys=True) for k, v in data["surfaces"].items()}


def test_scoped_update_changes_only_the_named_entrys_bytes(scoped, tmp_path):
    """On a canonical file the update's raw bytes are the old file's plus the one new entry."""
    raw = {m: (tmp_path / f"{m}.json").read_text() for m in ("3.10", "3.11")}
    surfaces = [s for s in harness.SURFACES if not s.tracked]
    assert harness._update_every_minor(surfaces, only=["dispatch_project_warm"]) == 0
    for minor, text in raw.items():
        after = (tmp_path / f"{minor}.json").read_text()
        entry = json.loads(after)["surfaces"]["dispatch_project_warm"]
        old = json.loads(text)
        old["surfaces"]["dispatch_project_warm"] = entry
        assert after == json.dumps(old, indent=2, sort_keys=True) + "\n"
        ops = difflib.SequenceMatcher(a=text.splitlines(), b=after.splitlines()).get_opcodes()
        assert {op[0] for op in ops} == {"equal", "insert"}, "only the new entry's lines"


def test_scoped_update_refuses_a_file_that_is_not_canonical(scoped, tmp_path, capsys):
    """A hand-formatted file would be re-serialised, so its kept entries' bytes would move."""
    path = tmp_path / "3.11.json"
    path.write_text(json.dumps(json.loads(path.read_text()), indent=4) + "\n")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    surfaces = [s for s in harness.SURFACES if not s.tracked]
    assert harness._update_every_minor(surfaces, only=["help_project_warm"]) == 1
    out = capsys.readouterr().out
    assert "3.11.json" in out, out
    assert "not in canonical form" in out, out
    assert "nothing written" in out, out
    assert scoped == [], "nothing is measured"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_scoped_update_leaves_other_entries_byte_identical(scoped, tmp_path):
    before = {m: _entries(tmp_path / f"{m}.json") for m in ("3.10", "3.11")}
    surfaces = [s for s in harness.SURFACES if not s.tracked]
    assert harness._update_every_minor(surfaces, only=["dispatch_project_warm"]) == 0
    assert scoped == [
        ("3.10", harness.RATIO_FLOOR), ("3.10", "dispatch_project_warm"),
        ("3.11", harness.RATIO_FLOOR), ("3.11", "dispatch_project_warm"),
    ], "the named surface and the floor its target ratio divides by"  # fmt: skip
    for minor in ("3.10", "3.11"):
        after = _entries(tmp_path / f"{minor}.json")
        assert json.loads(after.pop("dispatch_project_warm"))["file_ops"] == 500
        assert harness.RATIO_FLOOR not in after, "the floor is measured, never written"
        assert after == before[minor], "every other entry is byte-identical"
        text = (tmp_path / f"{minor}.json").read_text()
        assert json.loads(text)["interpreter"] == minor
    assert sorted(p.name for p in tmp_path.iterdir()) == ["3.10.json", "3.11.json"]


def test_scoped_update_of_a_surface_with_no_target_measures_no_floor(scoped, tmp_path):
    surfaces = [s for s in harness.SURFACES if not s.tracked]
    assert harness._update_every_minor(surfaces, only=["help_project_warm"]) == 0
    assert scoped == [("3.10", "help_project_warm"), ("3.11", "help_project_warm")]


@pytest.mark.parametrize(
    ("key", "why"),
    [("no_such_surface", "no surface named"), ("tracked_host_probe", "tracked")],
    ids=["unknown", "tracked"],
)
def test_scoped_update_refuses_an_unknown_or_tracked_key(scoped, tmp_path, capsys, key, why):
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    surfaces = [s for s in harness.SURFACES if not s.tracked]
    assert harness._update_every_minor(surfaces, only=["help_project_warm", key]) == 1
    out = capsys.readouterr().out
    assert why in out, out
    assert key in out, out
    assert "nothing written" in out
    assert scoped == [], "nothing is measured before every key is known"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_scoped_update_refuses_a_file_it_cannot_merge_into(scoped, tmp_path, capsys):
    (tmp_path / "3.11.json").write_text("any content\n")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    surfaces = [s for s in harness.SURFACES if not s.tracked]
    assert harness._update_every_minor(surfaces, only=["help_project_warm"]) == 1
    out = capsys.readouterr().out
    assert "3.11.json" in out, out
    assert "nothing written" in out, out
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_only_is_accepted_with_update_alone(monkeypatch, capsys):
    monkeypatch.setattr(harness, "measure_surface", lambda _s: pytest.fail("measured"))
    with pytest.raises(SystemExit) as exc:
        harness.main(["--check", "--only", "run"])
    assert exc.value.code == 2
    assert "--only" in capsys.readouterr().err


def test_update_passes_only_through(monkeypatch):
    seen = {}

    def update(surfaces, *, only=None):
        seen["only"] = only
        return 0

    monkeypatch.setattr(harness, "_update_every_minor", update)
    assert harness.main(["--update", "--only", "run, help_project_warm"]) == 0
    assert seen["only"] == ["run", "help_project_warm"]
    assert harness.main(["--update"]) == 0
    assert seen["only"] is None
