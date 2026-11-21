"""Deterministic import-budget guard.

See ``docs/superpowers/specs/2026-06-29-import-budget-guard-design.md``.
"""

import contextlib
import fcntl
import json
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from tests._fixtures.budget_harness import load_harness

harness = load_harness()


def test_measure_returns_module_inventory():
    result = harness.measure(["python"])
    assert result["count"] > 0
    assert "otto" in result["otto_modules"]
    # otto_modules is a strict subset of modules, sorted.
    assert set(result["otto_modules"]) <= set(result["modules"])
    assert result["modules"] == sorted(result["modules"])
    # non_stdlib_modules is the gated metric: a subset of modules that always
    # includes otto itself and never the standard library.
    assert set(result["non_stdlib_modules"]) <= set(result["modules"])
    assert "otto" in result["non_stdlib_modules"]


def test_a_stdlib_module_the_interpreter_does_not_list_is_not_counted_as_foreign():
    """CPython 3.15's ``traceback`` imports a module its own stdlib list omits."""
    names = [*sorted(harness.UNLISTED_STDLIB), "os.path", "otto", "typer.main"]
    classified = harness._run_child(
        harness.NON_STDLIB_HELPER + f"import json\nprint(json.dumps(_non_stdlib({names!r})))"
    )
    assert json.loads(classified) == ["otto", "typer.main"]


@pytest.mark.parametrize(
    "child", ["_CHILD_IMPORT", "_CHILD_CLI", "_CHILD_ENTRY", "_CHILD_BASELINE"]
)
def test_every_child_classifies_through_the_one_helper(child):
    source = getattr(harness, child)
    assert source.count("stdlib_module_names") == 1
    assert source.count("_non_stdlib(") == 2  # the definition and its one call


def test_surfaces_table_well_formed():
    keys = [s.key for s in harness.SURFACES]
    assert len(keys) == len(set(keys)), "surface keys must be unique"
    gated = {
        "import_otto",
        "help",
        "run",
        "host",
        "reservation",
        "docker",
        "schema",
        "monitor",
        "test",
        "cov",
        "run_bootstrapped",
        "version_project",
        "help_project",
        "help_project_warm",
        "bootstrap_project",
        "dispatch_project_warm",
        "completion_project_warm",
        "completion_project_handover",
        "test_project",
        "host_local_exec",
        "host_local_put",
        "host_local_get",
        "host_ssh_exec",
        "host_ssh_login",
        "dispatch_local_warm",
    }
    tracked = {
        *(
            f"tracked_{verb}"
            for verb in [
                "init",
                "env",
                "cache",
                "docker",
                "link",
                "tunnel",
                "monitor",
                "cov",
                "reservation",
                "inventory",
                "lab",
                "schema",
            ]
        ),
        "tracked_host_probe",
        "tracked_host_power",
        "tracked_tab_warm_templated",
    }
    assert set(keys) == gated | tracked
    assert {s.key for s in harness.SURFACES if s.tracked} == tracked
    # The prefix is how a reader of the table, and of a ceilings file, tells
    # a tier at a glance: a tracked key never appears in a ceilings file.
    assert all(key.startswith("tracked_") for key in tracked)
    assert not any(key.startswith("tracked_") for key in gated)


def test_the_run_host_and_test_surfaces_carry_a_target_ratio():
    """The verbs a user runs day to day have a target; ``otto --version`` is what they divide by.

    ``host_ssh_login`` stands in for the login path: ``LocalHost`` has no
    interactive session, so there is no local login surface to target.
    """
    targeted = {s.key for s in harness.SURFACES if s.target_ratio is not None}
    assert targeted == {
        "dispatch_project_warm",
        "host_local_exec",
        "host_local_put",
        "host_local_get",
        "host_ssh_exec",
        "host_ssh_login",
        "test_project",
        "dispatch_local_warm",
    }
    assert not any(harness.surface_by_key(key).tracked for key in targeted)
    floor = harness.surface_by_key(harness.RATIO_FLOOR)
    assert not floor.tracked
    assert floor.target_ratio is None


def test_exactly_two_surfaces_cover_the_composition_root():
    """The bootstrap-inclusive surfaces must exist, and the lazy ones stay lazy.

    Kills the blind spot the first of them was added for: every other surface
    resolves a dispatch target WITHOUT calling `bootstrap()`, so bootstrap-time
    imports went unmeasured. Deleting `bootstrap=True` from the table (or
    letting `measure_surface` drop the flag) restores that hole silently — the
    ceilings would simply be regenerated smaller — so the presence of the
    surfaces is asserted here rather than inferred from a passing budget.

    THE PAIR IS EXACT, AND ORDERED. ``run_bootstrapped`` bootstraps ZERO projects
    — that is what isolates the root's own import graph — and
    ``bootstrap_project`` is its project-bearing sibling. A third entry, or either
    one silently gaining/losing its project, changes what the pair measures.
    """
    bootstrapped = [s for s in harness.SURFACES if s.bootstrap]
    assert [s.key for s in bootstrapped] == ["run_bootstrapped", "bootstrap_project"]
    assert harness.surface_by_key("run_bootstrapped").project_files is None
    assert harness.surface_by_key("bootstrap_project").project_files == 50
    # And the flag has to reach the child, or the surface measures its twin.
    assert (
        harness.measure_surface(bootstrapped[0])["otto_modules"]
        != harness.measure(bootstrapped[0].argv)["otto_modules"]
    )


def test_check_surface_flags_a_real_entry_failure_exit():
    """A real-entry surface that exits non-zero measured a failure path, not the surface.

    Deleting this branch would let a crashing command pass the ceilings
    silently: they only see the file operations a broken run happened to
    produce, never that it was broken. ``exit_code`` is injected here rather
    than produced by an actually failing surface — every real surface in the
    table is meant to end as it expects — so this is the one place that
    failure path is exercised on a real measurement.
    """
    surface = harness.surface_by_key("version_project")
    result = harness.measure_surface(surface)
    failed = dict(result, exit_code=1)
    violations = harness.check_surface(surface, failed)
    assert any("measured a failure path" in v for v in violations), violations


@pytest.fixture(scope="session")
def ratio_floor_file_ops() -> int:
    """``otto --version``'s file operations, measured once per test process.

    Every target ratio divides by it. Session-scoped and requested only by a
    surface that has a target, so each xdist worker measures it at most once,
    in the same environment as the surfaces it divides.
    """
    return harness.measure_file_ops(harness.surface_by_key(harness.RATIO_FLOOR)).total


@pytest.mark.parametrize("surface", harness.SURFACES, ids=lambda s: s.key)
def test_import_budget(surface, request):
    """The gate: every surface measured ONCE, gated ones against their ceilings.

    A tracked surface is never gated, but it still has to run the command it
    names: a crash would print a meaningless row in ``make profile``'s table,
    so its ending is checked here, the one place it is measured in the suite.

    The surface-specific facts ride the same measurement rather than a second
    pass: a transfer surface's file must really arrive, and the SSH surfaces
    speak SSH in process (asyncssh), so no ``ssh`` program runs.

    A surface with a target ratio is also checked against ``otto --version``'s
    file operations (``ratio_floor_file_ops``).
    """
    result = harness.measure_surface(surface)
    if surface.tracked:
        violations = harness.check_exit(surface, result)
    else:
        floor = None
        if surface.target_ratio is not None:
            floor = request.getfixturevalue("ratio_floor_file_ops")
        violations = harness.check_surface(surface, result, floor_file_ops=floor)
    assert not violations, "\n".join(violations)
    assert result["file_ops"]["total"] > 0, result["file_ops"]
    if surface.key in ("host_local_put", "host_local_get"):
        dest = "put-dest" if surface.key == "host_local_put" else "get-dest"
        assert (harness.fixture_root(surface) / dest / "payload.txt").is_file()
    if surface.ssh_lab:
        assert "ssh" not in result["file_ops"]["by_process"], result["file_ops"]["by_process"]


def test_measure_reports_io_counts():
    """The payload's I/O is strace's file operations, in the shape the ceilings store."""
    result = harness.measure(["python"])
    ops = result["file_ops"]
    assert set(ops) == {"total", "workspace", "by_bucket", "by_process", "by_path"}
    assert isinstance(ops["total"], int)
    assert isinstance(ops["workspace"], int)
    # Importing otto touches files. Zero here means strace observed nothing.
    assert ops["total"] > 0
    # The breakdowns partition the total: every counted call lands in exactly
    # one bucket and one process.
    assert sum(ops["by_bucket"].values()) == ops["total"]
    assert sum(ops["by_process"].values()) == ops["total"]
    assert sum(ops["by_path"].values()) == ops["total"]
    # ...and this child has neither a fixture nor an OTTO_HOME (a bare
    # `measure` call passes the sanitized env, which carries no OTTO_*), so
    # there is no workspace to count.
    assert ops["workspace"] == 0
    # The audit-hook counters are gone: strace is the only I/O measurement.
    assert "io" not in result


def test_monitor_server_still_resolves():
    # PEP 562 lazy export must still work for library users.
    result = harness.measure(["python"])
    assert "fastapi" not in result["modules"]
    out = harness._run_child(
        "import json; from otto.monitor import MonitorServer; "
        "print(json.dumps([MonitorServer.__name__]))"
    )
    assert json.loads(out) == ["MonitorServer"]


def test_suite_public_api_still_resolves():
    out = harness._run_child(
        "import json; from otto.suite import OttoFixturesPlugin, run_tests; "
        "print(json.dumps(['ok']))"
    )
    assert json.loads(out) == ["ok"]


def test_bare_import_otto_is_lazy():
    """Bare `import otto` must not eagerly pull the CLI/config graph (Part D)."""
    code = (
        "import json, sys; import otto; "
        "print(json.dumps(['otto.cli' in sys.modules, "
        "'otto.config' in sys.modules, "
        "'otto.context' in sys.modules]))"
    )
    out = harness._run_child(code)
    assert json.loads(out) == [False, False, False], out


def test_library_use_populates_registries():
    """Lazy __init__ must not leave host/transfer registries empty for library
    users: accessing the lab API pulls otto.host, whose backends self-register."""
    code = (
        "import json; import otto; "
        "from otto import all_hosts; "  # triggers config -> host graph
        "from otto.host.transfer.registry import TRANSFER_BACKENDS; "
        "TRANSFER_BACKENDS.get('scp'); TRANSFER_BACKENDS.get('tftp'); "
        "print(json.dumps(['registries OK']))"
    )
    out = harness._run_child(code)
    assert json.loads(out) == ["registries OK"], out


def test_the_measured_venv_is_runtime_only_with_the_checkout_s_otto():
    """Two dev-only packages (hypothesis, a pytest plugin; playwright) are absent; otto is ours.

    #1150: otto must be this checkout's, installed editable.

    Asked of the measured child itself, the interpreter every surface runs:
    a dev package there would load into every pytest surface, and an otto
    from anywhere else would bucket as site-packages, not as otto's.
    """
    code = (
        "import importlib.util, json; "
        "print(json.dumps({n: importlib.util.find_spec(n) is not None "
        "for n in ('hypothesis', 'playwright')} | "
        "{'otto': importlib.util.find_spec('otto').origin}))"
    )
    found = json.loads(harness._run_child(code))
    assert (found["hypothesis"], found["playwright"]) == (False, False), found
    otto = Path(found["otto"]).resolve()
    assert otto.is_relative_to(harness.REPO_ROOT / "src"), f"{otto} is not the checkout's otto"


# --- Project-bearing surfaces (spec 2026-09-01, Phase 0) -----------------------
#
# Every surface above strips OTTO_* and resolves a dispatch target WITHOUT
# running `entry()`, so OTTOPATH is empty, discovery walks nothing, and both
# bootstrap-time project I/O and the completion caches are structurally invisible.
# The surfaces below run the real console entry path against a GENERATED project,
# which is what makes the startup-I/O work measurable at all.


def test_project_bearing_surface_isolates_home_and_project():
    """A project-bearing surface must bring its own OTTOPATH and OTTO_HOME.

    AND ITS BYTECODE PIN, on EVERY such surface — which is a gate, not
    housekeeping. ``PYTHONDONTWRITEBYTECODE`` keeps the fixture tree's own
    ``__pycache__`` from existing, and the tree is cached per process
    (by the harness's per-surface generator), so without the pin the first child in a process
    pays the `.pyc` probe misses and the writeback and every later one does
    not. Measured on ``bootstrap_project`` with the pin dropped, with the
    fixture-scoped open counter this harness used before its ceilings: 10,
    then 4, then 4 — order-dependent inside one run, and the gated
    ``workspace`` count carries those same probes. Asserted here rather than
    trusted to a comment.
    """
    surface = harness.surface_by_key("version_project")
    env = harness.surface_env(surface)

    assert Path(env["OTTOPATH"]).is_dir()
    assert "OTTO_HOME" in env
    # CONTAINMENT, not inequality. `!= ~/.otto` is satisfied by any path under
    # it — `~/.otto/home-<uuid>` included — which is a home that writes into
    # the developer's real one while reading as "not the real one".
    assert Path.home() not in Path(env["OTTO_HOME"]).parents

    unpinned = [
        s.key
        for s in harness.SURFACES
        if s.project_files is not None
        and harness.surface_env(s).get("PYTHONDONTWRITEBYTECODE") != "1"
    ]
    assert not unpinned, (
        f"project-bearing surfaces writing bytecode into the fixture tree: {unpinned} — "
        f"their gated workspace count becomes order-dependent"
    )


def test_every_surface_reads_the_harness_bytecode_cache_and_writes_none(monkeypatch):
    """Every child reads the harness's bytecode cache and writes none, whatever the runner says.

    ``tests/conftest.py`` exports a session-wide ``PYTHONPYCACHEPREFIX`` so no
    test process writes ``__pycache__`` into the editable ``src/otto`` tree
    (#321, #343, #360, #361), and ``_sanitized_env`` copies the ambient
    environment. A child that inherited it would count whatever that cache
    holds: cold on a fresh CI runner, warm on a developer's machine, and
    about one file operation apart per module. See
    :func:`scripts.import_budget.surface_env`.

    The variables are SET here rather than read off the runner: under a bare
    ``python scripts/import_budget.py`` no prefix exists, so a test that only
    inherited one would be green on every machine with the override deleted.
    """
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", "/nowhere/a-prefix-a-child-must-not-see")
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)

    wrong = {
        s.key: (env.get("PYTHONPYCACHEPREFIX"), env.get("PYTHONDONTWRITEBYTECODE"))
        for s in harness.SURFACES
        for env in [harness.surface_env(s)]
        if (env.get("PYTHONPYCACHEPREFIX"), env.get("PYTHONDONTWRITEBYTECODE"))
        != (harness.bytecode_prefix(), "1")
    }
    assert not wrong, f"surfaces not reading the harness's bytecode cache read-only: {wrong}"


def test_a_count_does_not_follow_the_runners_bytecode_cache(tmp_path, monkeypatch):
    """A cold cache in the runner's environment must not reach a measurement.

    The hostile condition is a fresh CI runner: the session's bytecode cache
    is empty, so the first child to import a module pays its compile. Before
    the harness owned the cache, a non-project surface inherited the runner's
    and wrote into it, so its first measurement in a session was hundreds of
    file operations dearer than its second.
    """
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(tmp_path / "cold-runner-cache"))
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    surface = harness.surface_by_key("run")
    first = harness.measure_surface(surface)["file_ops"]["total"]
    second = harness.measure_surface(surface)["file_ops"]["total"]
    assert abs(first - second) <= harness.MIN_SLACK, (first, second)


def test_a_pydantic_plugin_setting_in_the_runner_does_not_reach_a_count(monkeypatch):
    """The measured command decides ``PYDANTIC_DISABLE_PLUGINS``, never the runner.

    The ``otto`` command sets it itself (``otto._shim.main``), and turning the
    plugin scan off saves ~140 file operations on any child that builds a
    model. A runner that happens to carry the variable (an in-process test
    that ran the shim's fall-through, a developer's shell) must not hand that
    saving to a child that would not have had it. ``host`` is the cheapest
    surface whose child builds a model. Two measurements may differ by the
    FileFinder-refill noise ``MIN_SLACK`` absorbs, which is far below that
    saving.
    """
    surface = harness.surface_by_key("host")
    monkeypatch.delenv("PYDANTIC_DISABLE_PLUGINS", raising=False)
    without = harness.measure_file_ops(surface).total
    monkeypatch.setenv("PYDANTIC_DISABLE_PLUGINS", "__all__")
    with_var = harness.measure_file_ops(surface).total
    assert abs(with_var - without) <= harness.MIN_SLACK, (without, with_var)
    assert "PYDANTIC_DISABLE_PLUGINS" not in harness._sanitized_env()


def test_every_surface_pins_a_private_otto_home():
    """``OTTO_HOME`` is pinned on EVERY surface, not only the project-bearing ones.

    The gated ``workspace`` count attributes by prefix against ``$OTTO_HOME``.
    A surface that does not pin one therefore fails twice over: the child
    resolves ``~/.otto``, so whatever home I/O it performs lands on the
    runner's real home — machine state, and the count carries whatever that
    box happens to hold — and there is no home prefix, so none of that I/O
    is charged to the workspace. Either alone disqualifies the counter as a
    gate.

    Asserts the three properties that make the pin real: present, OUTSIDE the
    runner's home entirely, and FRESH per call (two calls to one surface must
    not share a home, or a warm cache would leak between measurements — the
    property ``Surface.warm`` is built on).

    The second one is CONTAINMENT rather than inequality, and the difference
    is the whole assertion. ``!= ~/.otto`` is satisfied by every path UNDER
    ``~/.otto`` — ``~/.otto/home-<uuid>`` is a plausible future spelling of
    "fresh per call", and it would pass all three checks while every measured
    child wrote its cache into the developer's real home.
    """
    homes = {}
    for surface in harness.SURFACES:
        env = harness.surface_env(surface)
        assert "OTTO_HOME" in env, f"`{surface.key}` does not pin OTTO_HOME"
        assert Path.home() not in Path(env["OTTO_HOME"]).parents, (
            f"`{surface.key}` puts its home inside the runner's: {env['OTTO_HOME']}"
        )
        homes[surface.key] = env["OTTO_HOME"]

    # Fresh per call, on every surface — the invariant `Surface.warm` depends on.
    repeats = {s.key: harness.surface_env(s)["OTTO_HOME"] for s in harness.SURFACES}
    shared = [key for key, home in homes.items() if repeats[key] == home]
    assert not shared, f"surfaces reusing one OTTO_HOME across calls: {shared}"


def test_version_does_not_read_the_corpus():
    """`otto --version` must not read the project at all.

    Before the console-script shim: 601 opens at 50 test files, growing ONE PER
    TEST FILE — a delta of 150 between a 200-file corpus and a 50-file one.
    After: a delta of 0, because the shim answers `--version` off
    `otto.version` alone and never bootstraps.

    The delta is the load-bearing form. An absolute bound alone is satisfied
    by a broken harness: if OTTOPATH stopped reaching the child, or the
    fixture generation silently produced nothing, the count would also be low
    and this would pass for the wrong reason. The delta can only be zero
    because the corpus size stopped mattering.
    """
    import dataclasses

    base = harness.surface_by_key("version_project")
    large = dataclasses.replace(base, key="version_project_large", project_files=200)
    small_ops = harness.measure_surface(base)["file_ops"]
    large_ops = harness.measure_surface(large)["file_ops"]
    delta = large_ops["workspace"] - small_ops["workspace"]
    assert delta == 0, f"--version still scales with the corpus: {small_ops} -> {large_ops}"


def test_project_bearing_surfaces_actually_find_the_generated_project():
    """The liveness pin: the harness's env injection reaches a child that finds a real project.

    A project-bearing surface whose ``workspace`` count is what a project-less run
    would count is not measuring a project at all — the env injection failed,
    and every ceiling on the project-bearing surfaces would pass for the wrong
    reason.

    SCOPED TO ``help_project``. ``version_project`` legitimately stops reading the
    project (the shim answers ``--version``), and the warm surfaces answer from
    the cache. Every measurement is cold by construction (a fresh
    ``OTTO_HOME`` per call), so a cold ``--help`` takes the full load, and
    the full load stats every path the ``names`` digest keys on. It reads no
    test file (a rebuild does not), so the bound is a CONTROL: the same
    command pointed at a project that does not exist, plus one operation per
    ``names`` key path inside the generated project. A stat of the ``OTTO_HOME``
    directory alone cannot clear it.
    """
    from otto.config.cache_sections import section_by_name
    from otto.config.project import Project

    surface = harness.surface_by_key("help_project")
    live = harness.measure_surface(surface)["file_ops"]["workspace"]

    env = harness.surface_env(surface)
    project_root = Path(env["OTTOPATH"])
    env["OTTOPATH"] = str(project_root.parent / "no-such-project")
    dead = harness.measure(
        surface.argv, bootstrap=surface.bootstrap, real_entry=surface.real_entry, env=env
    )["file_ops"]["workspace"]

    keys = set(section_by_name("names").key_paths([Project(root=project_root)]))
    in_project = [p for p in keys if project_root in p.parents]
    assert in_project, "the generated project declares no settings or init module"
    assert live >= dead + len(in_project), (
        f"help_project counted {live}, a project-less run {dead}: the project was not found"
    )


def test_project_bearing_surface_does_not_grow_sys_path():
    """``add_libs_to_pythonpath`` prepends each project's lib dirs before every
    later import probe — a regression class module count cannot see.

    Counts the ``sys.path`` entries that live UNDER THE FIXTURE ROOT rather
    than the total length. A total is a budget on the whole interpreter: it
    moves with editable-vs-wheel installs, with layout changes, and with any
    dev dependency that ships a ``.pth`` — so it fails for reasons outside the
    regression it names, and raising it to buy headroom would stop it catching
    a second project's lib dir, the exact thing it is for.

    WAS ``== 1`` UNTIL TASK 4, AND THE TRANSITION IS THE PROOF. The 1 was the
    project's ``pylib``, put on the path by ``add_libs_to_pythonpath`` during the
    bootstrap that answering ``--version`` used to require. It was observed
    green at 1 before the shim landed and red at 0 after, so this ``== 0`` is
    a witnessed flip rather than a value written to match whatever the code
    happened to produce.

    That flip CONSUMES this test's own liveness argument, though: an
    always-zero ``_fixture_path_entries`` would now satisfy both this and its
    ``== 0`` companion below. The replacement guarantee is
    ``test_fixture_path_entries_counts_the_paths_it_is_given``, which drives
    the counter directly and is task-order independent.

    Deliberately NOT extended to the help surfaces. Cold ``help_project`` still
    bootstraps and so still reports 1 — correctly, because a full load has to
    put the project's ``pylib`` on the path to import its init module — while
    ``help_project_warm`` reports 0. Asserting the pair belongs to the surface
    that measures the cached path, not to this counter's own liveness pin.
    """
    result = harness.measure_surface(harness.surface_by_key("version_project"))
    entries = result["fixture_path_entries"]
    assert entries == 0, f"{entries} sys.path entries came from the project under measurement"


def test_non_project_surfaces_report_no_fixture_path_entries():
    """The other end of the counter: 0 where there is no fixture at all."""
    assert harness.measure_surface(harness.surface_by_key("run"))["fixture_path_entries"] == 0


def test_fixture_path_entries_counts_the_paths_it_is_given(monkeypatch):
    """PERMANENT liveness pin for ``_fixture_path_entries`` — drive it directly.

    Every OTHER assertion on this counter now expects 0: ``version_project`` stops
    growing ``sys.path`` at Task 4, ``help_project`` at Task 7, and non-project
    surfaces never did. Once that is true everywhere, replacing the counter's
    body with ``return 0`` is undetectable — a dead instrument that later
    acceptance criteria still lean on. This test is the one place that
    observes it counting NON-ZERO, and it depends on no task's state.

    Runs the harness's real preamble source (never a hand-copied
    reimplementation, which would drift) against a synthetic ``sys.path``.
    """
    import sys as _sys

    root = "/synthetic/fixture/root"
    monkeypatch.setenv(harness.FIXTURE_ROOT_ENV_VAR, root)
    monkeypatch.setattr(
        _sys,
        "path",
        [
            root,  # the root itself counts
            root + "/pylib",  # a lib dir under it counts
            root + "/a/b/c",  # nested counts
            root + "-sibling",  # shares the PREFIX but is not under it
            "/unrelated",  # nothing to do with the fixture
        ],
    )
    namespace = {}
    exec(harness._CHILD_PREAMBLE, namespace)  # noqa: S102 — the harness's own source
    assert namespace["_fixture_path_entries"]() == 3

    # And the early-out branch: no fixture root in the env means 0 whatever
    # sys.path holds — which is why the 0s elsewhere cannot prove liveness.
    monkeypatch.delenv(harness.FIXTURE_ROOT_ENV_VAR)
    bare = {}
    exec(harness._CHILD_PREAMBLE, bare)  # noqa: S102 — the harness's own source
    assert bare["_fixture_path_entries"]() == 0


def test_name_only_surfaces_execute_no_test_modules():
    """Importing a project's test files to answer --version/--help is the defect.

    A test file is a ``test_*.py`` module, so a module of that name in
    ``sys.modules`` is a direct, cheap signal.

    ``help_project_warm``, not ``help_project``: the name-only promise is about the
    CACHED path — the one a second and every later ``otto --help`` takes.
    """
    for key in ("version_project", "help_project_warm"):
        mods = harness.measure_surface(harness.surface_by_key(key))["modules"]
        test_mods = [m for m in mods if m.rpartition(".")[2].startswith("test_")]
        assert test_mods == [], f"{key} executed test modules: {test_mods}"


def test_help_io_does_not_scale_with_corpus_size():
    """The cached help path must be O(1) in corpus size, not merely under a ceiling.

    A ceiling only catches a constant growing. Two measurements taken in one
    environment differ only by the corpus, so their DELTA says whether the
    cached path pays per file: ``workspace`` is the corpus's own reads, stats
    and listings, and the whole-process ``file_ops`` delta catches the same
    walk reaching the corpus by a path outside the workspace prefix. Its bound
    is looser, because it also carries the import system's run-to-run wobble.

    Gates the WARM variants. Cold help still walks and still reads per file,
    by design; the guarantee is that the SECOND run stops paying for a corpus
    it never reports on.
    """
    import dataclasses

    base = harness.surface_by_key("help_project_warm")
    small = dataclasses.replace(base, key="help_small", project_files=50, project_dirs_count=5)
    large = dataclasses.replace(base, key="help_large", project_files=200, project_dirs_count=20)

    ops_small = harness.measure_surface(small)["file_ops"]
    ops_large = harness.measure_surface(large)["file_ops"]

    assert ops_large["workspace"] - ops_small["workspace"] <= 5, (
        f"help workspace I/O scales with corpus: "
        f"{ops_small['workspace']} -> {ops_large['workspace']}"
    )
    assert ops_large["total"] - ops_small["total"] <= 15, (
        f"help file ops scale with corpus: {ops_small['total']} -> {ops_large['total']}"
    )


def test_a_warm_surface_is_seeded_and_repeats_identically(tmp_path):
    """``warm=True`` must actually seed, and warm measurement must be repeatable.

    Two properties, and the first is what keeps the second honest. Determinism
    alone cannot detect a broken seed: two COLD measurements agree too (every
    measurement is independent, and ``PYTHONDONTWRITEBYTECODE`` keeps the
    fixture tree from warming), so ``a == b`` would pass unchanged if
    ``measure_surface`` stopped running the seed altogether. The strict
    inequality against the cold twin is the half that fails when it does.

    The two surfaces have the same shape and therefore byte-identical
    generated corpora; only the seed differs.

    REPEATABILITY IS ASSERTED ON ``workspace``, NOT ON ``file_ops``. The
    whole-process total counts every probe the import machinery makes, and a
    module whose ``.pyc`` is missing costs more of them than a cached one.
    The harness reads a cache of its own that no child writes, but any cache
    is state outside the per-call ``OTTO_HOME``, so the total is the counter
    that would move if some process wrote into it (issue #321 was a test
    comparing it). ``workspace`` must not move even then.

    So the hostile condition is INJECTED here rather than waited for. The two
    measurements read a throwaway cache in ``tmp_path`` (so the real one is
    never touched), and a process that writes bytecode fills it between
    them. ``workspace`` must not move; the total MUST, or the injection has
    quietly stopped biting and the independence being asserted is no longer
    being tested at all.
    """
    import dataclasses

    def redirect_bytecode_cache(surface):
        """Point *surface*'s child at the throwaway cache, keeping its own extras."""
        return dataclasses.replace(
            surface,
            env_extra=(*surface.env_extra, ("PYTHONPYCACHEPREFIX", str(tmp_path / "bytecode"))),
        )

    warm = redirect_bytecode_cache(harness.surface_by_key("help_project_warm"))
    first = harness.measure_surface(warm)["file_ops"]
    # The perturbation: a process filling the bytecode cache mid-flight, by
    # importing what root help imports with bytecode writing on.
    writer_env = dict(harness._sanitized_env(), PYTHONPYCACHEPREFIX=str(tmp_path / "bytecode"))
    writer_env.pop("PYTHONDONTWRITEBYTECODE", None)
    harness._run_child("import otto.cli.main, rich.markdown; print('[]')", writer_env)
    second = harness.measure_surface(warm)["file_ops"]

    assert first["workspace"] == second["workspace"], (
        f"repeat warm measurements disagree on the workspace count: "
        f"{first['workspace']} vs {second['workspace']}"
    )
    assert first["total"] != second["total"], (
        f"the bytecode-cache injection did not bite ({first['total']} both times), so this "
        f"test no longer proves the workspace count is independent of that cache"
    )

    # The cold twin is measured WITHOUT the redirect, as every other test
    # measures it: this arm compares `workspace`, which counts otto's reads of
    # the corpus and which no bytecode cache outside the tree can move.
    cold = harness.measure_surface(harness.surface_by_key("help_project"))["file_ops"]
    assert first["workspace"] < cold["workspace"], (
        f"the seed run left nothing behind: warm {first} vs cold {cold}"
    )


def test_surface_by_key_refuses_an_unknown_key():
    """Never index SURFACES positionally — the lookup must be by name."""
    with pytest.raises(KeyError):
        harness.surface_by_key("no_such_surface")


def test_hyperfine_is_gone():
    """Wall-clock never gated and nothing ran it; the tool and its plumbing are removed."""
    from tests._fixtures.paths import CHECKOUT_ROOT

    hits = []
    for rel in ["Makefile", "scripts", "docs/contributing.md", "docs/architecture", "tests/unit"]:
        root = CHECKOUT_ROOT / rel
        files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        for f in files:
            if f.suffix == ".pyc" or f.name == "test_import_budget.py":
                continue
            try:
                if "hyperfine" in f.read_text(errors="ignore").lower():
                    hits.append(str(f.relative_to(CHECKOUT_ROOT)))
            except OSError:
                continue
    assert not hits, f"hyperfine still referenced: {hits}"


# --- File-operation ceilings --------------------------------------------------
#
# The release gate's instrument. A wall-clock number fails for reasons outside
# the change (load, thermals, page cache) and so can only ever be monitoring.
# File-operation counts repeat run to run and are what actually predicted a
# real NFS deployment, so they are what gates.


def test_every_gated_surface_has_a_ceiling_for_this_interpreter():
    """A missing baseline is a NAMED failure — never a silent skip.

    Cheap and direct, ahead of the measuring gate: the ceilings are keyed per
    Python minor, so "this interpreter has no file" is the shape a newly added
    interpreter (or a newly added surface) takes, and it must not be possible
    for a leg to report green having compared nothing. The other direction
    too: a tracked surface, or one that no longer exists, has no business in
    the file.
    """
    recorded = harness.read_ceilings()
    gated = {s.key for s in harness.SURFACES if not s.tracked}
    missing = sorted(gated - set(recorded))
    assert not missing, (
        f"no baseline on CPython {harness.interpreter_tag()} for {missing} — "
        f"regenerate with {harness.SNAPSHOT_HOW}"
    )
    extra = sorted(set(recorded) - gated)
    assert not extra, f"baselines for surfaces that are not gated: {extra}"


def test_the_child_runs_in_a_private_empty_cwd(tmp_path, monkeypatch):
    """The child's cwd is the harness's own empty directory, never the caller's.

    ``python -c`` puts the cwd on ``sys.path``, so whatever directory the child
    starts in is one it imports from. Inheriting the caller's cwd hands it the
    checkout root, which every other test process in the run writes into.
    """
    monkeypatch.chdir(tmp_path)
    body = """
import json, os
print(json.dumps({"cwd": os.getcwd(), "entries": os.listdir(".")}))
"""
    child = json.loads(harness._run_child(harness._CHILD_PREAMBLE + body))

    assert Path(child["cwd"]).resolve() != tmp_path.resolve(), child
    assert child["entries"] == [], child


def test_a_caller_writing_into_its_cwd_does_not_move_a_count(tmp_path, monkeypatch):
    """A sibling writing into the caller's cwd mid-measurement must not reach the child.

    CPython's ``FileFinder`` re-lists a ``sys.path`` directory whenever its
    mtime moved, and ``python -c`` puts the cwd on ``sys.path``. With an
    inherited cwd, a sibling bumping its mtime made the child re-list it
    mid-measurement: ``help_project_warm`` on CPython 3.14 read one more than
    its baseline (#428). The child now starts in the harness's own empty
    directory, so the caller's cwd is not on its path at all.

    The hostile condition is INJECTED rather than left to chance: a thread
    moves the caller's cwd mtime forward every few milliseconds for the whole
    measurement, seed run included, and the whole-process count must equal
    an undisturbed measurement's from the same cwd. The total, not
    ``workspace``: the caller's cwd is under neither the fixture root nor
    ``OTTO_HOME``, so a re-listing of it is charged to the total alone, and a
    warm surface's total repeats exactly run to run.
    """
    import os
    import threading
    import time

    surface = harness.surface_by_key("help_project_warm")
    monkeypatch.chdir(tmp_path)
    quiet = harness.measure_surface(surface)["file_ops"]
    stop = threading.Event()

    def bump() -> None:
        stamp = time.time()
        while not stop.is_set():
            stamp += 1
            os.utime(tmp_path, (stamp, stamp))
            time.sleep(0.005)

    bumper = threading.Thread(target=bump)
    bumper.start()
    try:
        bumped = harness.measure_surface(surface)["file_ops"]
    finally:
        stop.set()
        bumper.join()

    assert bumped["total"] == quiet["total"], (quiet, bumped)


def test_bootstrap_project_is_the_project_bearing_sibling():
    """The pair's whole value is that one of them still sees NO project.

    ``run_bootstrapped`` isolates the composition root's own import graph, and
    can only do that against an empty workspace: the moment it grows a project,
    the difference between the two stops being "what a workspace costs at
    bootstrap" and the older surface's count silently starts including it.

    The project is witnessed by the workspace I/O bootstrap still pays (reading
    the project's settings, putting its lib dirs on the path, importing its init
    tree); no test file is imported outside a pytest session.
    """
    empty = harness.measure_surface(harness.surface_by_key("run_bootstrapped"))["file_ops"]
    assert empty["workspace"] == 0, empty

    with_project = harness.measure_surface(harness.surface_by_key("bootstrap_project"))["file_ops"]
    assert with_project["workspace"] > 0, with_project


def test_completion_env_reaches_the_child():
    """``env_extra`` is what makes the completion surface a completion surface.

    Without it the same argv is a bare ``otto``, which renders the root help
    screen — so the surface would keep its name, keep passing, and measure
    something else entirely. The inequality is what fails when the field stops
    reaching the child.

    ON THE MODULE COUNT, NOT ON I/O, and the failure that produced this test
    is the reason: the warm completion surface loads 3 non-stdlib modules and
    walks nothing — the shim answers the TAB straight from the cache without ever
    importing ``otto.cli`` — while bare ``otto`` (no ``_OTTO_COMPLETE`` in
    the env) falls through the shim and renders the root help through the
    full CLI path, pulling in the whole command tree. What separates them is
    that fork in the shim itself, which the module count sees directly and
    the workspace I/O, correctly, does not have to.
    """
    surface = harness.surface_by_key("completion_project_warm")
    env = harness.surface_env(surface)
    assert env["_OTTO_COMPLETE"] == "complete_bash"

    import dataclasses

    plain = dataclasses.replace(surface, key="completion_no_env", env_extra=())
    completing = harness.measure_surface(surface)["non_stdlib_modules"]
    full = harness.measure_surface(plain)["non_stdlib_modules"]
    assert len(completing) < len(full), (len(completing), len(full))


def test_completion_io_does_not_scale_with_corpus_size():
    """The steady-state TAB cost must be O(1) in corpus size, not merely under a ceiling.

    The same guarantee ``test_help_io_does_not_scale_with_corpus_size`` makes
    for warm root help, for the surface a user hits most often, with the same
    two deltas. A DELTA between two measurements taken in one environment is
    comparable where an absolute number is not: whatever the bytecode cache
    and the installed dists add, they add to both sides. The shim's stat pass
    is over the NAMES key set on this site, which is O(top-level).
    """
    import dataclasses

    base = harness.surface_by_key("completion_project_warm")
    small = dataclasses.replace(
        base, key="completion_small", project_files=50, project_dirs_count=5
    )
    large = dataclasses.replace(
        base, key="completion_large", project_files=200, project_dirs_count=20
    )

    ops_small = harness.measure_surface(small)["file_ops"]
    ops_large = harness.measure_surface(large)["file_ops"]

    assert ops_large["workspace"] - ops_small["workspace"] <= 5, (
        f"completion workspace I/O scales with corpus: "
        f"{ops_small['workspace']} -> {ops_large['workspace']}"
    )
    assert ops_large["total"] - ops_small["total"] <= 15, (
        f"completion file ops scale with corpus: {ops_small['total']} -> {ops_large['total']}"
    )


def test_completion_handover_io_does_not_scale_with_corpus_size():
    """The FALLBACK's corpus cost must be O(1) too — the shim's cheapness buys nothing here.

    ``completion_project_warm``'s sibling test above pins the shim's own answer
    path, which reads one JSON file and cannot witness a corpus-scaling walk
    by construction. ``completion_project_handover`` is the other side of the
    same TAB: a `live` site the shim hands over on, which falls through to
    the unchanged full CLI path — the one that resolves ``tunnel remove``'s
    own module tree and DOES walk the generated project to discover it. A
    corpus-size regression on that walk is exactly the kind of thing the
    shim's cheapness on the warm path would otherwise let slip past
    unnoticed, so it needs its own pin.
    """
    import dataclasses

    base = harness.surface_by_key("completion_project_handover")
    small = dataclasses.replace(
        base, key="completion_handover_small", project_files=50, project_dirs_count=5
    )
    large = dataclasses.replace(
        base, key="completion_handover_large", project_files=200, project_dirs_count=20
    )

    ops_small = harness.measure_surface(small)["file_ops"]
    ops_large = harness.measure_surface(large)["file_ops"]

    assert ops_large["workspace"] - ops_small["workspace"] <= 5, (
        f"completion handover workspace I/O scales with corpus: "
        f"{ops_small['workspace']} -> {ops_large['workspace']}"
    )
    assert ops_large["total"] - ops_small["total"] <= 15, (
        f"completion handover file ops scale with corpus: "
        f"{ops_small['total']} -> {ops_large['total']}"
    )


def test_dispatch_io_does_not_scale_with_corpus_size():
    """A real command must not pay for the test corpus before doing its own work.

    The branch of entry() every ordinary command takes: bootstrap, then
    dispatch. Nothing on it reads the completion cache, so nothing on it may
    walk or stat the corpus: the same two deltas as the help and TAB pins,
    with the same tolerance.
    """
    import dataclasses

    base = harness.surface_by_key("dispatch_project_warm")
    small = dataclasses.replace(base, key="dispatch_small", project_files=50, project_dirs_count=5)
    large = dataclasses.replace(
        base, key="dispatch_large", project_files=200, project_dirs_count=20
    )

    ops_small = harness.measure_surface(small)["file_ops"]
    ops_large = harness.measure_surface(large)["file_ops"]

    assert ops_large["workspace"] - ops_small["workspace"] <= 5, (
        f"dispatch workspace I/O scales with corpus: "
        f"{ops_small['workspace']} -> {ops_large['workspace']}"
    )
    assert ops_large["total"] - ops_small["total"] <= 15, (
        f"dispatch file ops scale with corpus: {ops_small['total']} -> {ops_large['total']}"
    )


def test_cold_rebuild_does_not_scale_with_corpus_size():
    """A cold rebuild reads nothing of the test corpus: O(1), like every other help and TAB.

    Cold ``help_project`` rebuilds the ``names`` and ``shim`` sections, whose key
    sets are the settings, the init trees and the lab files. Test names come
    only from pytest's collections (the per-file table), so a rebuild neither
    walks nor stats a test file: between 50 files/5 dirs and 200 files/20
    dirs its workspace I/O holds, with the same tolerance as the warm pins.
    """
    import dataclasses

    base = harness.surface_by_key("help_project")
    small = dataclasses.replace(base, key="rebuild_small", project_files=50, project_dirs_count=5)
    large = dataclasses.replace(base, key="rebuild_large", project_files=200, project_dirs_count=20)
    ops_small = harness.measure_surface(small)["file_ops"]
    ops_large = harness.measure_surface(large)["file_ops"]
    assert ops_large["workspace"] - ops_small["workspace"] <= 5, (
        f"cold rebuild workspace I/O scales with corpus: "
        f"{ops_small['workspace']} -> {ops_large['workspace']}"
    )


def test_a_test_name_tab_does_not_scale_with_corpus_size():
    """A test-name TAB answered by the shim is O(1) in the corpus: it never stats the test tree.

    The seed is the same TAB on a cold home: it hands over, and the full path
    rebuilds the sections and waits once for the collect child's whole-tree
    table, which starts the check window. The measured TAB is answered by the
    shim from that table (no typer): it reads the table and stats the
    ``env`` (the pytest configs, the settings, site-packages), and the check
    of what the table tracks is the detached collect child's, once a window
    has lapsed. Between 50 files/5 dirs and 200 files/20 dirs its I/O holds,
    with the tolerance the other O(1) pins use (Chris, 2026-09-28: a warm
    TAB's file ops must not grow with the corpus; on NFS each is a round
    trip).
    """
    import dataclasses

    base = harness.surface_by_key("completion_project_warm")
    tab = (("_OTTO_COMPLETE", "complete_bash"), ("COMP_WORDS", "otto test T"), ("COMP_CWORD", "2"))
    small = dataclasses.replace(
        base, key="tests_tab_small", project_files=50, project_dirs_count=5, env_extra=tab
    )
    large = dataclasses.replace(
        base, key="tests_tab_large", project_files=200, project_dirs_count=20, env_extra=tab
    )
    measured_small = harness.measure_surface(small)
    measured_large = harness.measure_surface(large)
    for measured in (measured_small, measured_large):
        loaded = measured["non_stdlib_modules"]
        assert not any(m == "typer" or m.startswith("typer.") for m in loaded), (
            "the shim did not answer the measured TAB"
        )
    ops_small, ops_large = measured_small["file_ops"], measured_large["file_ops"]
    assert ops_large["workspace"] - ops_small["workspace"] <= 5, (
        f"a test-name TAB's workspace I/O scales with the corpus: "
        f"{ops_small['workspace']} -> {ops_large['workspace']}"
    )
    assert ops_large["total"] - ops_small["total"] <= 15, (
        f"a test-name TAB's file ops scale with the corpus: "
        f"{ops_small['total']} -> {ops_large['total']}"
    )


def test_env_extra_is_applied_after_the_sanitizer(monkeypatch):
    """A surface may deliberately set an ``OTTO_*`` var the sanitizer strips.

    That ordering is the whole reason ``env_extra`` is applied last, and the
    completion surface depends on the mechanism (though not on this particular
    collision). Unexercised, swapping the two lines in ``surface_env`` would be
    invisible: every current surface's vars survive either order. This drives
    the collision directly — an ambient ``OTTO_*`` the sanitizer removes, set
    again by ``env_extra`` — so sanitizing LAST reds it.
    """
    import dataclasses

    monkeypatch.setenv("OTTO_ENV_EXTRA_PROBE", "ambient")
    base = harness.surface_by_key("run")

    # Baseline: the sanitizer really does strip it, so the assertion below is
    # about ordering rather than about the var simply being present.
    assert "OTTO_ENV_EXTRA_PROBE" not in harness.surface_env(base)

    override = dataclasses.replace(
        base, key="env_extra_probe", env_extra=(("OTTO_ENV_EXTRA_PROBE", "from-env-extra"),)
    )
    assert harness.surface_env(override)["OTTO_ENV_EXTRA_PROBE"] == "from-env-extra"


def test_the_report_rows_carry_by_path_and_the_ceilings_do_not(tmp_path, monkeypatch):
    ops = harness.FileOps(
        total=3,
        workspace=0,
        by_bucket={"stdlib": 3},
        by_process={"python": 3},
        by_path={"stdlib:os.py": 2, "stdlib:": 1},
    )
    gated = next(s for s in harness.SURFACES if not s.tracked)
    row = harness.report_row(gated, {"file_ops": asdict(ops), "exit_code": 0})
    assert row["by_path"] == {"stdlib:os.py": 2, "stdlib:": 1}
    monkeypatch.setattr(harness, "CEILINGS_DIR", tmp_path)
    harness.write_ceilings("9.98", {gated.key: ops})
    stored = json.loads(harness.ceilings_path("9.98").read_text())["surfaces"][gated.key]
    assert "by_path" not in stored
    assert stored["by_bucket"] == {"stdlib": 3}


# --- The measured interpreter and the venv builder (#1147) -------------------
#
# Pure: no strace, no uv, no measured child. Each test that switches the
# measured interpreter restores whatever was measured before (`measured`).


@pytest.fixture
def measured(monkeypatch):
    """Let a test call ``use_python`` freely: the seam and its caches are put back afterwards."""
    for name, fresh in [
        ("_MEASURED", None), ("_WARMED", set()), ("_BUILT", {}), ("_FACTS", {}), ("_BASELINES", {}),
        ("_BUILD_FAILED", {}),
    ]:  # fmt: skip
        monkeypatch.setattr(harness, name, fresh)


def _completed(argv, returncode=0):
    return subprocess.CompletedProcess(argv, returncode, "", "")


def test_the_measured_interpreter_defaults_to_the_running_minor_s_built_venv(
    measured, tmp_path, monkeypatch
):
    """Built on the first ask, for the minor running the harness; build_env's memo keeps it."""
    syncs = []
    monkeypatch.setattr(harness, "default_venv", lambda minor, _tree: tmp_path / minor)
    monkeypatch.setattr(harness.subprocess, "run", lambda argv, **_kw: _sync(syncs, argv))
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    expected = str(tmp_path / running / "bin" / "python")
    assert harness.measured_python() == expected
    assert harness.measured_python() == expected
    assert syncs == [harness.sync_argv(running)], "one sync per process"
    harness.use_python("/chosen/bin/python")
    assert harness.measured_python() == "/chosen/bin/python", "a choice overrides the default"
    assert len(syncs) == 1


def _sync(syncs, argv):
    syncs.append(argv)
    return _completed(argv)


def test_the_first_measurement_builds_its_venv_before_it_takes_the_venv_lock(
    measured, tmp_path, monkeypatch
):
    """A fresh process's first ``measure()`` resolves the interpreter, THEN locks.

    The other order would sync under its own shared lock: a
    :class:`NestedVenvLockError` at best, a wait on itself forever at worst.
    """
    real_build = harness.build_env
    venv = tmp_path / ".venv-budget" / f"{sys.version_info[0]}.{sys.version_info[1]}"
    lock = venv.with_name(venv.name + ".lock")
    exclusive, shared = harness.HeldLock(lock, shared=False), harness.HeldLock(lock, shared=True)
    events = []

    class MeasuredError(Exception):
        pass

    def sync(argv, **_kw):
        events.append(("sync", list(harness._HELD_LOCKS)))
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("")
        return _completed(argv)

    def child(*_a, **_kw):
        events.append(("child", list(harness._HELD_LOCKS)))
        raise MeasuredError

    monkeypatch.setattr(harness, "build_env", lambda minor: real_build(minor, tree=tmp_path))
    monkeypatch.setattr(harness.subprocess, "run", sync)
    monkeypatch.setattr(harness, "_run_child", child)
    with pytest.raises(MeasuredError):
        harness.measure(["python"])
    assert harness.measured_python() == str(venv / "bin" / "python")
    assert events == [("sync", [exclusive]), ("child", [shared])], (
        "the sync under its own exclusive lock, released; then the measurement under the shared one"
    )
    assert harness._HELD_LOCKS == []


def test_use_python_switches_the_interpreter_and_clears_every_per_interpreter_cache(
    measured, monkeypatch
):
    """After a switch, facts, the baseline and the warm-up are each asked of the new interpreter."""
    children = []
    compilers = []

    def child(code, env=None, **_kw):
        children.append((code, harness.measured_python()))
        if code == harness._CHILD_BASELINE:
            return "[]"
        return json.dumps({"minor": "9.97", "cache_tag": "cpython-997", "import_dirs": []})

    def run(argv, **_kw):
        compilers.append(argv[0])
        return _completed(argv)

    monkeypatch.setattr(harness, "_run_child", child)
    monkeypatch.setattr(harness.subprocess, "run", run)

    def ask_everything():
        harness.measured_facts()
        harness.baseline_modules()
        # No otto in the (empty) import dirs, so the warm-up refuses; it still compiled.
        with pytest.raises(RuntimeError, match="writable"):
            harness._warm_bytecode()

    harness.use_python("/fake/a/bin/python")
    ask_everything()
    harness.use_python("/fake/b/bin/python")
    assert harness.measured_python() == "/fake/b/bin/python"
    ask_everything()
    asked_b = [python for code, python in children[2:]]
    assert asked_b == ["/fake/b/bin/python"] * 2, children
    assert {code for code, _ in children[2:]} == {harness._CHILD_FACTS, harness._CHILD_BASELINE}
    assert compilers == ["/fake/a/bin/python", "/fake/b/bin/python"]


def test_the_interpreter_tag_and_bytecode_tag_are_the_measured_interpreter_s(monkeypatch):
    """A 3.10 harness measuring 3.14 keys 3.14's file and reads 3.14's ``.pyc`` names."""
    monkeypatch.setattr(
        harness, "measured_facts", lambda: {"minor": "9.97", "cache_tag": "cpython-997"}
    )
    assert harness.interpreter_tag() == "9.97"
    assert harness.cached_bytecode("/cache", Path("/src/otto/__init__.py")) == Path(
        "/cache/src/otto/__init__.cpython-997.pyc"
    )


def test_the_bytecode_warmup_compiles_with_the_measured_interpreter(measured, monkeypatch):
    """The warm-up's "done" memo is per interpreter, and its compiler is the measured one."""
    compilers = []

    def run(argv, **_kw):
        compilers.append(argv[0])
        return _completed(argv)

    monkeypatch.setattr(harness, "measured_facts", lambda: {"import_dirs": []})
    monkeypatch.setattr(harness.subprocess, "run", run)
    for python in ("/fake/a/bin/python", "/fake/b/bin/python"):
        harness.use_python(python)
        # No otto in the (empty) import dirs, so the warm-up refuses; it still compiled.
        with pytest.raises(RuntimeError, match="writable"):
            harness._warm_bytecode()
    assert compilers == ["/fake/a/bin/python", "/fake/b/bin/python"]


def test_a_warm_cache_is_compiled_once_per_interpreter(measured, tmp_path, monkeypatch):
    """A warm-up that lands otto's ``.pyc`` is remembered; a new interpreter compiles again."""
    src = tmp_path / "src"
    (src / "otto").mkdir(parents=True)
    otto_init = src / "otto" / "__init__.py"
    otto_init.write_text("")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(
        harness,
        "measured_facts",
        lambda: {"import_dirs": [str(src)], "cache_tag": "cpython-997", "minor": "9.97"},
    )
    compilers = []

    def compile_run(argv, **_kw):
        compilers.append(argv[0])
        cached = harness.cached_bytecode(harness.bytecode_prefix(), otto_init)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(b"")
        return _completed(argv)

    monkeypatch.setattr(harness.subprocess, "run", compile_run)
    harness.use_python("/fake/a/bin/python")
    harness._warm_bytecode()
    harness._warm_bytecode()
    assert compilers == ["/fake/a/bin/python"], "a warm cache is not compiled twice"
    harness.use_python("/fake/b/bin/python")
    harness._warm_bytecode()
    assert compilers == ["/fake/a/bin/python", "/fake/b/bin/python"], "a new interpreter compiles"


def test_the_measured_children_and_the_sync_drop_the_same_caller_paths(
    measured, tmp_path, monkeypatch
):
    """One contaminated caller env, both entry points: neither keeps the caller's Python paths."""
    monkeypatch.setenv("PYTHONPATH", "/caller/site")
    monkeypatch.setenv("PYTHONHOME", "/caller/home")
    monkeypatch.setenv("VIRTUAL_ENV", "/caller/.venv")
    monkeypatch.setenv("UV_PROJECT", "/elsewhere/project")
    monkeypatch.setenv("UV_WORKING_DIR", "/elsewhere")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/elsewhere/env")
    monkeypatch.setenv("UV_CACHE_DIR", "/cache")
    monkeypatch.setenv("OTTO_LAB", "caller-lab")
    harness.use_python(str(tmp_path / "bare" / "python"))
    child, sync = harness._sanitized_env(), harness.sync_env(tmp_path / "venv")
    for env in (child, sync):
        for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT", "UV_WORKING_DIR"):
            assert name not in env, name
        assert env["UV_CACHE_DIR"] == "/cache", "cache settings pass through"
    assert "UV_PROJECT_ENVIRONMENT" not in child
    assert sync["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / "venv"), "set after the strip"
    assert set(child) <= set(harness.venv_clean()), "the child's env is venv_clean's, filtered"
    assert "OTTO_LAB" not in child


def test_the_measured_venv_is_active_in_every_child_env(measured, tmp_path, monkeypatch):
    """``VIRTUAL_ENV`` names the measured interpreter's venv and its ``bin`` leads ``PATH``."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("")
    monkeypatch.setenv("VIRTUAL_ENV", "/caller/.venv")
    monkeypatch.setenv("PATH", "/caller/.venv/bin:/usr/bin")
    harness.use_python(str(venv / "bin" / "python"))
    env = harness._sanitized_env()
    assert env["VIRTUAL_ENV"] == str(venv)
    assert env["PATH"] == f"{venv / 'bin'}:/caller/.venv/bin:/usr/bin"
    assert harness.activated_env(env, venv) == env, "activating twice changes nothing"

    harness.use_python(str(tmp_path / "bare" / "python"))
    assert "VIRTUAL_ENV" not in harness._sanitized_env(), "no venv, so none is named"


def test_build_env_syncs_the_tree_s_lock_into_the_venv_once_per_process(
    measured, tmp_path, monkeypatch
):
    calls = []

    def run(argv, *, cwd, env, check, timeout):
        calls.append((argv, cwd, env))
        return _completed(argv)

    monkeypatch.setenv("VIRTUAL_ENV", "/caller/.venv")
    monkeypatch.setattr(harness.subprocess, "run", run)
    tree = tmp_path / "tree"
    tree.mkdir()
    python = harness.build_env("3.13", tree=tree)
    venv = tree / ".venv-budget" / "3.13"
    assert python == venv / "bin" / "python"
    [(argv, cwd, env)] = calls
    assert argv == ["uv", "sync", "--frozen", "--no-default-groups", "--python", "3.13"]
    assert "--no-install-project" not in argv, "otto stays installed, so its files bucket as otto's"
    assert argv == harness.sync_argv("3.13")
    assert cwd == tree, "the measured tree's own uv.lock"
    assert env["UV_PROJECT_ENVIRONMENT"] == str(venv)
    assert "VIRTUAL_ENV" not in env, "uv must not sync the caller's venv"
    assert harness.build_env("3.13", tree=tree) == python
    assert len(calls) == 1, "one sync per venv per process"
    other = tmp_path / "holder" / "venv-3.13"
    assert harness.build_env("3.13", tree=tree, venv=other) == other / "bin" / "python"
    assert calls[-1][2]["UV_PROJECT_ENVIRONMENT"] == str(other)


def test_a_failed_sync_is_a_named_error_remembered_for_the_process(measured, tmp_path, monkeypatch):
    syncs = []

    def run(argv, **_kw):
        syncs.append(argv)
        return _completed(argv, 2)

    monkeypatch.setattr(harness.subprocess, "run", run)
    with pytest.raises(harness.EnvBuildError, match=r"uv sync .*3\.13.* exited 2") as first:
        harness.build_env("3.13", tree=tmp_path)
    assert harness._BUILT == {}, "a failed venv is never handed out"
    with pytest.raises(harness.EnvBuildError) as again:
        harness.build_env("3.13", tree=tmp_path)
    assert again.value is first.value, "a later ask re-raises the same failure"
    assert len(syncs) == 1, "without syncing again"


def test_a_sync_that_runs_too_long_is_a_named_failure(measured, tmp_path, monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen["timeout"] = kw.get("timeout")
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(harness.subprocess, "run", run)
    with pytest.raises(harness.EnvBuildError, match=r"timed out after 1200s"):
        harness.build_env("3.13", tree=tmp_path)
    assert seen["timeout"] == harness.SYNC_TIMEOUT


_SHARED_HOLDER = """
import fcntl, sys, time
from pathlib import Path
lock, marker = sys.argv[1], Path(sys.argv[2])
with open(lock, "a") as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
    print("held", flush=True)
    time.sleep(0.5)
    marker.write_text("released")
"""


def test_a_live_measurement_s_shared_lock_holds_off_a_sync(measured, tmp_path, monkeypatch):
    """Another process measuring the venv holds its lock shared; the sync waits for it."""
    tree = tmp_path / "tree"
    tree.mkdir()
    venv = tree / ".venv-budget" / "3.13"
    lock = venv.with_name("3.13.lock")
    lock.parent.mkdir(parents=True)
    marker = tmp_path / "released"
    seen = []

    def run(argv, **_kw):
        seen.append(marker.exists())
        return _completed(argv)

    monkeypatch.setattr(harness.subprocess, "run", run)
    with subprocess.Popen(
        [sys.executable, "-c", _SHARED_HOLDER, str(lock), str(marker)],
        stdout=subprocess.PIPE,
        text=True,
    ) as holder:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        harness.build_env("3.13", tree=tree)
    assert seen == [True], "the sync ran only once the measurement let go"


_LOOPING_HOLDER = """
import importlib.util, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location("budget_harness", sys.argv[1])
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
venv, log = Path(sys.argv[2]), Path(sys.argv[3])
cycles, period, offset = int(sys.argv[4]), float(sys.argv[5]), float(sys.argv[6])
print("ready", flush=True)
start = float(sys.stdin.readline().split()[1]) + offset
time.sleep(max(0.0, start - time.monotonic()))
for cycle in range(cycles):
    with harness._venv_lock(venv, shared=True):
        with log.open("a") as out:
            out.write("x")
        if cycle == 0:
            print("held", flush=True)
        # Hold until this cycle's end on a fixed schedule, so the two
        # readers' holds stay half a period apart and always overlap.
        time.sleep(max(0.0, start + (cycle + 1) * period - time.monotonic()))
"""
"""A measurement after measurement through the harness's own lock (so through its
turnstile), as back-to-back measurements of one worker take it. It says "ready",
waits for "go <t0>", then holds the lock shared from ``t0 + offset`` on a fixed
schedule of *period*-long holds, each taken again as soon as the last one ends."""


@pytest.mark.timeout(60)
def test_overlapping_measurements_never_starve_a_sync(measured, tmp_path, monkeypatch):
    """Two workers keep the venv shared without a gap; a sync that asks gets in at once.

    The two readers hold the lock on schedules half a period apart, so one of
    them always holds it. Both are started, both have held it, and only then
    does the sync ask. Without the turnstile in :func:`_venv_lock`, Linux
    grants each new shared lock past the waiting exclusive one, so the sync
    waits for every remaining cycle. With it, the sync waits only for the
    holds already under way: at most one new shared acquisition per reader
    between its request and its grant.
    """
    tree = tmp_path / "tree"
    tree.mkdir()
    venv = tree / ".venv-budget" / "3.13"
    venv.parent.mkdir(parents=True)
    log = tmp_path / "acquired"
    log.write_text("")
    cycles, period, readers = 20, 0.2, 2
    granted_at = []

    def run(argv, **_kw):
        granted_at.append(len(log.read_text()))
        return _completed(argv)

    monkeypatch.setattr(harness.subprocess, "run", run)
    base = [sys.executable, "-c", _LOOPING_HOLDER, harness.__file__, str(venv), str(log)]
    with contextlib.ExitStack() as stack:
        holders = []
        for index in range(readers):
            argv = [*base, str(cycles), str(period), str(index * period / readers)]
            holder = stack.enter_context(
                subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            )
            stack.callback(holder.kill)
            assert holder.stdin is not None
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "ready"
            holders.append(holder)
        go = time.monotonic() + 0.2
        for holder in holders:
            holder.stdin.write(f"go {go}\n")
            holder.stdin.flush()
        for holder in holders:
            assert holder.stdout.readline().strip() == "held"
        requested_at = len(log.read_text())
        harness.build_env("3.13", tree=tree)
    [granted] = granted_at
    assert requested_at < readers * cycles - readers, "the readers were still cycling"
    assert granted - requested_at <= readers, (
        f"{granted - requested_at} shared holds were granted between the sync's request and "
        f"its grant ({requested_at} -> {granted}); with the turnstile at most one per reader"
    )


def test_a_measurement_holds_its_venv_s_lock_shared(measured, tmp_path, monkeypatch):
    venv = tmp_path / ".venv-budget" / "3.13"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("")
    lock = venv.with_name("3.13.lock")
    lock.write_text("")
    harness.use_python(str(venv / "bin" / "python"))
    outcomes = []

    class MeasuredError(Exception):
        pass

    def try_exclusive() -> bool:
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return True

    def child(*_a, **_kw):
        outcomes.append(try_exclusive())
        raise MeasuredError

    monkeypatch.setattr(harness, "_run_child", child)
    with pytest.raises(MeasuredError):
        harness.measure(["python"])
    assert outcomes == [False], "a sync cannot take the lock mid-measurement"
    assert try_exclusive(), "the lock is released when the measurement ends"


def test_find_interpreter_asks_uv_for_a_system_interpreter_venv_clean(monkeypatch):
    calls = []

    def run(argv, *, env, capture_output, text, check):
        calls.append((argv, env))
        return subprocess.CompletedProcess(argv, 0, "/usr/bin/python3.13\n", "")

    monkeypatch.setenv("VIRTUAL_ENV", "/caller/.venv")
    monkeypatch.setattr(harness.subprocess, "run", run)
    assert harness.find_interpreter("3.13") == "/usr/bin/python3.13"
    [(argv, env)] = calls
    assert argv == ["uv", "python", "find", "--system", "3.13"]
    assert "VIRTUAL_ENV" not in env
    monkeypatch.setattr(harness.subprocess, "run", lambda argv, **_kw: _completed(argv, 2))
    assert harness.find_interpreter("3.13") is None


def test_update_takes_no_single_interpreter(monkeypatch, capsys):
    monkeypatch.setattr(harness, "measure_surface", lambda _s: pytest.fail("measured"))
    with pytest.raises(SystemExit) as exc:
        harness.main(["--update", "--python", "/usr/bin/python3"])
    assert exc.value.code == 2
    assert "every interpreter" in capsys.readouterr().err


def _gated_results(total: int) -> dict:
    ops = {"total": total, "workspace": 1, "by_bucket": {}, "by_process": {}, "by_path": {}}
    return {"exit_code": 0, "exception": None, "file_ops": ops}


def _update_world(monkeypatch, tmp_path, minors, *, missing=(), fail_on=None):
    """Make ``--update`` see *minors*' ceilings files and measure them without strace or uv."""
    floor = harness.surface_by_key(harness.RATIO_FLOOR)
    surface = harness.surface_by_key("run")
    monkeypatch.setattr(harness, "CEILINGS_DIR", tmp_path)
    for minor in minors:
        (tmp_path / f"{minor}.json").write_text(f'{{"interpreter": "{minor}"}}\n')
    monkeypatch.setattr(
        harness, "find_interpreter", lambda m: None if m in missing else f"/usr/bin/python{m}"
    )
    built = []

    def build_env(minor, **_kw):
        built.append(minor)
        return Path(f"/budget/{minor}/bin/python")

    monkeypatch.setattr(harness, "build_env", build_env)
    monkeypatch.setattr(
        harness, "measured_facts", lambda: {"minor": harness.measured_python().split("/")[2]}
    )
    monkeypatch.setattr(harness, "SURFACES", [surface, floor])

    def measure_surface(s):
        minor = harness.measured_python().split("/")[2]
        result = _gated_results(100 + int(minor.split(".")[1]))
        if (minor, s.key) == fail_on:
            result["exit_code"] = 3
        return result

    monkeypatch.setattr(harness, "measure_surface", measure_surface)
    return built


def test_update_names_every_interpreter_it_cannot_find_and_writes_nothing(
    measured, tmp_path, monkeypatch, capsys
):
    built = _update_world(monkeypatch, tmp_path, ["3.10", "9.99", "9.98"], missing={"9.99", "9.98"})
    before = {p.name: p.read_text() for p in tmp_path.iterdir()}
    assert harness.main(["--update"]) == 1
    out = capsys.readouterr().out
    assert "no interpreter for 9.98 (uv python find --system 9.98)" in out
    assert "no interpreter for 9.99 (uv python find --system 9.99)" in out
    assert built == [], "nothing is built or measured before every interpreter is found"
    assert {p.name: p.read_text() for p in tmp_path.iterdir()} == before


def test_update_writes_each_interpreter_s_own_file(measured, tmp_path, monkeypatch, capsys):
    built = _update_world(monkeypatch, tmp_path, ["3.11", "3.10"])
    assert harness.main(["--update"]) == 0
    assert built == ["3.10", "3.11"], "sorted, each in its own venv"
    for minor in ("3.10", "3.11"):
        data = json.loads((tmp_path / f"{minor}.json").read_text())
        assert data["interpreter"] == minor
        assert data["surfaces"]["run"]["file_ops"] == 100 + int(minor.split(".")[1])
    out = capsys.readouterr().out
    assert "wrote tests/unit/import_budget" not in out, (
        "the redirected directory, not the checkout's"
    )
    for minor in ("3.10", "3.11"):
        assert f"wrote {tmp_path / minor}.json" in out, out


def test_a_failure_on_a_later_interpreter_leaves_every_file_untouched(
    measured, tmp_path, monkeypatch, capsys
):
    built = _update_world(
        monkeypatch, tmp_path, ["3.10", "3.11"], fail_on=("3.11", harness.RATIO_FLOOR)
    )
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert harness.main(["--update"]) == 1
    assert built == ["3.10", "3.11"], "3.10 measured clean before 3.11 failed"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert "nothing written" in capsys.readouterr().out


def test_a_failure_while_staging_leaves_every_file_byte_identical(
    measured, tmp_path, monkeypatch, capsys
):
    """Every minor measured clean, then staging the second file fails: no file changes."""
    _update_world(monkeypatch, tmp_path, ["3.10", "3.11"])
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    real_text = harness._ceilings_text

    def text(minor, results):
        if minor == "3.11":
            raise OSError("No space left on device")
        return real_text(minor, results)

    monkeypatch.setattr(harness, "_ceilings_text", text)
    assert harness.main(["--update"]) == 1
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before, "no staged file left"
    out = capsys.readouterr().out
    assert "No space left on device" in out
    assert "nothing written" in out


def test_a_failed_replacement_names_what_was_replaced_and_what_failed(
    measured, tmp_path, monkeypatch, capsys
):
    """The second rename fails: 3.10 is new, 3.11 is untouched, and the message says so."""
    _update_world(monkeypatch, tmp_path, ["3.10", "3.11"])
    before = (tmp_path / "3.11.json").read_bytes()
    real_replace = Path.replace

    def replace(self, target):
        if Path(target).name == "3.11.json":
            raise OSError("Read-only file system")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    assert harness.main(["--update"]) == 1
    out = capsys.readouterr().out
    assert "nothing written" not in out, "a file was written; the message must not deny it"
    assert "3.11.json failed (Read-only file system); replaced: " in out, out
    assert f"replaced: {tmp_path / '3.10.json'}" in out, out
    assert json.loads((tmp_path / "3.10.json").read_text())["interpreter"] == "3.10"
    assert "surfaces" in json.loads((tmp_path / "3.10.json").read_text())
    assert (tmp_path / "3.11.json").read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["3.10.json", "3.11.json"]


def test_a_failed_cleanup_never_hides_a_failed_replacement(measured, tmp_path, monkeypatch, capsys):
    """The second rename fails and so does removing its staged file: both are named."""
    _update_world(monkeypatch, tmp_path, ["3.10", "3.11"])
    staged = tmp_path / ".3.11.json.staged"
    real_replace, real_unlink = Path.replace, Path.unlink

    def replace(self, target):
        if Path(target).name == "3.11.json":
            raise OSError("Read-only file system")
        return real_replace(self, target)

    def unlink(self, missing_ok=False):
        if self == staged:
            raise PermissionError("Operation not permitted")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)
    assert harness.main(["--update"]) == 1
    out = capsys.readouterr().out
    assert "nothing written" not in out, out
    assert f"replaced: {tmp_path / '3.10.json'}" in out, out
    assert f"staged files left behind: {staged}" in out, out
    assert staged.exists(), "the file the cleanup could not remove is still there, as named"


def test_a_clean_update_leaves_no_staged_file(measured, tmp_path, monkeypatch):
    _update_world(monkeypatch, tmp_path, ["3.10", "3.11"])
    assert harness.main(["--update"]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["3.10.json", "3.11.json"]


def test_python_measures_the_named_interpreter_as_is(measured, monkeypatch):
    monkeypatch.setattr(harness, "build_env", lambda *_a, **_kw: pytest.fail("built"))
    seen = []
    surface = harness.surface_by_key(harness.RATIO_FLOOR)
    monkeypatch.setattr(harness, "SURFACES", [surface])

    def measure_surface(_s):
        seen.append(harness.measured_python())
        return _gated_results(1)

    monkeypatch.setattr(harness, "measure_surface", measure_surface)
    assert harness.main(["--python", "/some/venv/bin/python"]) == 0
    assert seen == ["/some/venv/bin/python"]


@pytest.mark.parametrize("argv", [[], ["--check"], ["--report"]])
def test_a_failed_build_is_a_named_exit_before_anything_is_measured(
    measured, monkeypatch, capsys, argv
):
    """The default venv is built before the table, so a sync failure is a message, not a trace."""

    def build_env(minor, **_kw):
        raise harness.EnvBuildError(f"import_budget: `uv sync` for {minor} exited 2")

    monkeypatch.setattr(harness, "build_env", build_env)
    monkeypatch.setattr(harness, "measure_surface", lambda _s: pytest.fail("measured"))
    assert harness.main(argv) == 1
    out, err = capsys.readouterr()
    assert "`uv sync` for " in err
    assert "exited 2" in err
    assert out == "", "no table header before the build"


def test_a_relative_python_is_made_absolute_but_never_resolved(measured, tmp_path, monkeypatch):
    """The measured children run in another cwd, and a venv's interpreter is a link."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("")
    harness.venv_python(venv).symlink_to(sys.executable)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(harness, "build_env", lambda *_a, **_kw: pytest.fail("built"))
    monkeypatch.setattr(harness, "SURFACES", [harness.surface_by_key(harness.RATIO_FLOOR)])
    seen = []

    def measure_surface(_s):
        seen.append(harness.measured_python())
        return _gated_results(1)

    monkeypatch.setattr(harness, "measure_surface", measure_surface)
    assert harness.main(["--python", "venv/bin/python"]) == 0
    assert seen == [str(harness.venv_python(venv))]
    assert harness.venv_of(seen[0]) == venv, "the link is kept, so its venv is still found"


@pytest.mark.timeout(30)
def test_build_env_under_a_held_venv_lock_refuses_instead_of_hanging(
    measured, tmp_path, monkeypatch
):
    """flock conflicts between two opens in ONE process, so nesting would wait forever."""
    venv = tmp_path / ".venv-budget" / "3.13"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("")
    venv.with_name("3.13.lock").write_text("")
    harness.use_python(str(harness.venv_python(venv)))
    monkeypatch.setattr(harness.subprocess, "run", lambda argv, **_kw: pytest.fail("synced"))
    with (
        harness._measurement_lock(),
        pytest.raises(harness.NestedVenvLockError, match=r"3\.13\.lock"),
    ):
        harness.build_env("3.13", tree=tmp_path)


@pytest.mark.timeout(30)
@pytest.mark.parametrize("inner_shared", [True, False])
def test_a_venv_lock_this_process_holds_is_refused_when_asked_again(tmp_path, inner_shared):
    """Even shared in shared would queue behind a sync waiting at the turnstile."""
    venv = tmp_path / ".venv-budget" / "3.13"
    with harness._venv_lock(venv, shared=True):
        with (
            pytest.raises(harness.NestedVenvLockError, match=r"3\.13\.lock"),
            harness._venv_lock(venv, shared=inner_shared),
        ):
            pytest.fail("nested lock taken")
        assert [h.path for h in harness._HELD_LOCKS] == [venv.with_name("3.13.lock")]
    assert harness._HELD_LOCKS == []


def _locked_venv(tmp_path):
    """A venv the builder made: its interpreter's dir, its pyvenv.cfg and its lock file."""
    venv = tmp_path / ".venv-budget" / "9.97"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("")
    lock = venv.with_name("9.97.lock")
    lock.write_text("")
    return venv, lock


def _a_sync_would_wait(lock) -> bool:
    """Whether an exclusive flock of *lock*, as a sync takes it, would wait right now."""
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def test_every_probe_of_the_measured_interpreter_runs_under_the_shared_venv_lock(
    measured, tmp_path, monkeypatch
):
    """The facts, the baseline and the warm-up's compile: a sync contending with each waits."""
    venv, lock = _locked_venv(tmp_path)
    harness.use_python(str(harness.venv_python(venv)))
    facts = {"minor": "9.97", "cache_tag": "cpython-997", "import_dirs": []}
    waited = {}

    def run(argv, **_kw):
        code = argv[argv.index("-c") + 1] if "-c" in argv else "compileall"
        kind = {harness._CHILD_FACTS: "facts", harness._CHILD_BASELINE: "baseline"}.get(code, code)
        waited[kind] = _a_sync_would_wait(lock)
        out = "[]" if kind == "baseline" else json.dumps(facts)
        return subprocess.CompletedProcess(argv, 0, out + "\n", "")

    monkeypatch.setattr(harness.subprocess, "run", run)
    harness.measured_facts()
    harness.baseline_modules()
    with pytest.raises(RuntimeError, match="writable"):  # no otto in the empty import dirs
        harness._warm_bytecode()
    assert waited == {"facts": True, "baseline": True, "compileall": True}
    assert harness._HELD_LOCKS == []
    assert not _a_sync_would_wait(lock), "every lock is let go"


def test_a_child_under_a_held_measurement_lock_reuses_it(measured, tmp_path, monkeypatch):
    """Reentrant: a probe inside ``measure()``'s lock takes no second flock and never refuses."""
    venv, lock = _locked_venv(tmp_path)
    harness.use_python(str(harness.venv_python(venv)))
    held = []

    def run(argv, **_kw):
        held.append(list(harness._HELD_LOCKS))
        return subprocess.CompletedProcess(argv, 0, "[]\n", "")

    monkeypatch.setattr(harness.subprocess, "run", run)
    with harness._measurement_lock():
        harness.baseline_modules()
    assert held == [[harness.HeldLock(lock, shared=True)]], "one shared lock, the outer one"
    assert harness._HELD_LOCKS == []


def test_a_stray_file_in_the_ceilings_directory_is_refused_by_name(
    measured, tmp_path, monkeypatch, capsys
):
    _update_world(monkeypatch, tmp_path, ["3.10"])
    (tmp_path / "notes.json").write_text("{}")
    with pytest.raises(ValueError, match=r"notes\.json"):
        harness.ceilings_minors()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert harness.main(["--update"]) == 1
    assert "notes.json" in capsys.readouterr().out
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
