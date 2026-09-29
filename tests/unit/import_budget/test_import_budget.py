"""Deterministic import-budget guard.

See ``docs/superpowers/specs/2026-06-29-import-budget-guard-design.md``.
"""

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
        "version_repo",
        "help_repo",
        "help_repo_warm",
        "bootstrap_repo",
        "dispatch_repo_warm",
        "completion_repo_warm",
        "completion_repo_handover",
        "test_repo",
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
                "schema",
            ]
        ),
        "tracked_host_probe",
        "tracked_host_power",
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
        "dispatch_repo_warm",
        "host_local_exec",
        "host_local_put",
        "host_local_get",
        "host_ssh_exec",
        "host_ssh_login",
        "test_repo",
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

    THE PAIR IS EXACT, AND ORDERED. ``run_bootstrapped`` bootstraps ZERO repos
    — that is what isolates the root's own import graph — and
    ``bootstrap_repo`` is its repo-bearing sibling. A third entry, or either
    one silently gaining/losing its repo, changes what the pair measures.
    """
    bootstrapped = [s for s in harness.SURFACES if s.bootstrap]
    assert [s.key for s in bootstrapped] == ["run_bootstrapped", "bootstrap_repo"]
    assert harness.surface_by_key("run_bootstrapped").sut_files is None
    assert harness.surface_by_key("bootstrap_repo").sut_files == 50
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
    surface = harness.surface_by_key("version_repo")
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
    assert set(ops) == {"total", "workspace", "by_bucket", "by_process"}
    assert isinstance(ops["total"], int)
    assert isinstance(ops["workspace"], int)
    # Importing otto touches files. Zero here means strace observed nothing.
    assert ops["total"] > 0
    # The breakdowns partition the total: every counted call lands in exactly
    # one bucket and one process.
    assert sum(ops["by_bucket"].values()) == ops["total"]
    assert sum(ops["by_process"].values()) == ops["total"]
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
    import subprocess
    import sys

    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from otto.monitor import MonitorServer; print(MonitorServer.__name__)",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=harness._sanitized_env(),
    )
    assert out.stdout.strip() == "MonitorServer"


def test_suite_public_api_still_resolves():
    import subprocess
    import sys

    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from otto.suite import OttoFixturesPlugin, run_tests; print('ok')",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=harness._sanitized_env(),
    )
    assert out.stdout.strip() == "ok"


def test_bare_import_otto_is_lazy():
    """Bare `import otto` must not eagerly pull the CLI/config graph (Part D)."""
    import subprocess
    import sys

    code = (
        "import sys; import otto; "
        "print('otto.cli' in sys.modules, "
        "'otto.config' in sys.modules, "
        "'otto.context' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env=harness._sanitized_env(),
    )
    assert out.stdout.strip() == "False False False", out.stdout


def test_library_use_populates_registries():
    """Lazy __init__ must not leave host/transfer registries empty for library
    users: accessing the lab API pulls otto.host, whose backends self-register."""
    import subprocess
    import sys

    code = (
        "import otto; "
        "from otto import all_hosts; "  # triggers config -> host graph
        "from otto.host.transfer.registry import build_transfer_backend; "
        "build_transfer_backend('scp'); build_transfer_backend('tftp'); "
        "print('registries OK')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env=harness._sanitized_env(),
    )
    assert out.stdout.strip() == "registries OK", out.stdout


# --- Repo-bearing surfaces (spec 2026-09-01, Phase 0) -----------------------
#
# Every surface above strips OTTO_* and resolves a dispatch target WITHOUT
# running `entry()`, so `sut_dirs` is empty, discovery walks nothing, and both
# bootstrap-time repo I/O and the completion caches are structurally invisible.
# The surfaces below run the real console entry path against a GENERATED repo,
# which is what makes the startup-I/O work measurable at all.


def test_repo_bearing_surface_isolates_home_and_repo():
    """A repo-bearing surface must bring its own OTTO_SUT_DIRS and OTTO_HOME.

    AND ITS BYTECODE PIN, on EVERY such surface — which is a gate, not
    housekeeping. ``PYTHONDONTWRITEBYTECODE`` keeps the fixture tree's own
    ``__pycache__`` from existing, and the tree is cached per process
    (``_generated_repo_for``), so without the pin the first child in a process
    pays the `.pyc` probe misses and the writeback and every later one does
    not. Measured on ``bootstrap_repo`` with the pin dropped, with the
    fixture-scoped open counter this harness used before its ceilings: 10,
    then 4, then 4 — order-dependent inside one run, and the gated
    ``workspace`` count carries those same probes. Asserted here rather than
    trusted to a comment.
    """
    surface = harness.surface_by_key("version_repo")
    env = harness.surface_env(surface)

    assert Path(env["OTTO_SUT_DIRS"]).is_dir()
    assert "OTTO_HOME" in env
    # CONTAINMENT, not inequality. `!= ~/.otto` is satisfied by any path under
    # it — `~/.otto/home-<uuid>` included — which is a home that writes into
    # the developer's real one while reading as "not the real one".
    assert Path.home() not in Path(env["OTTO_HOME"]).parents

    unpinned = [
        s.key
        for s in harness.SURFACES
        if s.sut_files is not None and harness.surface_env(s).get("PYTHONDONTWRITEBYTECODE") != "1"
    ]
    assert not unpinned, (
        f"repo-bearing surfaces writing bytecode into the fixture tree: {unpinned} — "
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
    the harness owned the cache, a non-repo surface inherited the runner's
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
    """``OTTO_HOME`` is pinned on EVERY surface, not only the repo-bearing ones.

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
    """`otto --version` must not read the repo at all.

    Before the console-script shim: 601 opens at 50 test files, growing ONE PER
    TEST FILE — a delta of 150 between a 200-file corpus and a 50-file one.
    After: a delta of 0, because the shim answers `--version` off
    `otto.version` alone and never bootstraps.

    The delta is the load-bearing form. An absolute bound alone is satisfied
    by a broken harness: if OTTO_SUT_DIRS stopped reaching the child, or the
    fixture generation silently produced nothing, the count would also be low
    and this would pass for the wrong reason. The delta can only be zero
    because the corpus size stopped mattering.
    """
    import dataclasses

    base = harness.surface_by_key("version_repo")
    large = dataclasses.replace(base, key="version_repo_large", sut_files=200)
    small_ops = harness.measure_surface(base)["file_ops"]
    large_ops = harness.measure_surface(large)["file_ops"]
    delta = large_ops["workspace"] - small_ops["workspace"]
    assert delta == 0, f"--version still scales with the corpus: {small_ops} -> {large_ops}"


def test_repo_bearing_surfaces_actually_find_the_generated_repo():
    """The liveness pin: the harness's env injection reaches a child that finds a real repo.

    A repo-bearing surface whose ``workspace`` count is what a repo-less run
    would count is not measuring a repo at all — the env injection failed,
    and every ceiling on the repo-bearing surfaces would pass for the wrong
    reason.

    SCOPED TO ``help_repo``. ``version_repo`` legitimately stops reading the
    repo (the shim answers ``--version``), and the warm surfaces answer from
    the cache. Every measurement is cold by construction (a fresh
    ``OTTO_HOME`` per call), so a cold ``--help`` takes the full load, and
    the full load stats every path the ``names`` digest keys on. It reads no
    test file (a rebuild does not), so the bound is a CONTROL: the same
    command pointed at a repo that does not exist, plus one operation per
    ``names`` key path inside the generated repo. A stat of the ``OTTO_HOME``
    directory alone cannot clear it.
    """
    from otto.config.cache_sections import section_by_name
    from otto.config.repo import Repo

    surface = harness.surface_by_key("help_repo")
    live = harness.measure_surface(surface)["file_ops"]["workspace"]

    env = harness.surface_env(surface)
    repo_dir = Path(env["OTTO_SUT_DIRS"])
    env["OTTO_SUT_DIRS"] = str(repo_dir.parent / "no-such-repo")
    dead = harness.measure(
        surface.argv, bootstrap=surface.bootstrap, real_entry=surface.real_entry, env=env
    )["file_ops"]["workspace"]

    keys = set(section_by_name("names").key_paths([Repo(sut_dir=repo_dir)]))
    in_repo = [p for p in keys if repo_dir in p.parents]
    assert in_repo, "the generated repo declares no settings or init module"
    assert live >= dead + len(in_repo), (
        f"help_repo counted {live}, a repo-less run {dead}: the repo was not found"
    )


def test_repo_bearing_surface_does_not_grow_sys_path():
    """``add_libs_to_pythonpath`` prepends each repo's lib dirs before every
    later import probe — a regression class module count cannot see.

    Counts the ``sys.path`` entries that live UNDER THE FIXTURE ROOT rather
    than the total length. A total is a budget on the whole interpreter: it
    moves with editable-vs-wheel installs, with layout changes, and with any
    dev dependency that ships a ``.pth`` — so it fails for reasons outside the
    regression it names, and raising it to buy headroom would stop it catching
    a second repo's lib dir, the exact thing it is for.

    WAS ``== 1`` UNTIL TASK 4, AND THE TRANSITION IS THE PROOF. The 1 was the
    repo's ``pylib``, put on the path by ``add_libs_to_pythonpath`` during the
    bootstrap that answering ``--version`` used to require. It was observed
    green at 1 before the shim landed and red at 0 after, so this ``== 0`` is
    a witnessed flip rather than a value written to match whatever the code
    happened to produce.

    That flip CONSUMES this test's own liveness argument, though: an
    always-zero ``_fixture_path_entries`` would now satisfy both this and its
    ``== 0`` companion below. The replacement guarantee is
    ``test_fixture_path_entries_counts_the_paths_it_is_given``, which drives
    the counter directly and is task-order independent.

    Deliberately NOT extended to the help surfaces. Cold ``help_repo`` still
    bootstraps and so still reports 1 — correctly, because a full load has to
    put the repo's ``pylib`` on the path to import its init module — while
    ``help_repo_warm`` reports 0. Asserting the pair belongs to the surface
    that measures the cached path, not to this counter's own liveness pin.
    """
    result = harness.measure_surface(harness.surface_by_key("version_repo"))
    entries = result["fixture_path_entries"]
    assert entries == 0, f"{entries} sys.path entries came from the repo under measurement"


def test_non_repo_surfaces_report_no_fixture_path_entries():
    """The other end of the counter: 0 where there is no fixture at all."""
    assert harness.measure_surface(harness.surface_by_key("run"))["fixture_path_entries"] == 0


def test_fixture_path_entries_counts_the_paths_it_is_given(monkeypatch):
    """PERMANENT liveness pin for ``_fixture_path_entries`` — drive it directly.

    Every OTHER assertion on this counter now expects 0: ``version_repo`` stops
    growing ``sys.path`` at Task 4, ``help_repo`` at Task 7, and non-repo
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
    """Importing a repo's test files to answer --version/--help is the defect.

    A test file is a ``test_*.py`` module, so a module of that name in
    ``sys.modules`` is a direct, cheap signal.

    ``help_repo_warm``, not ``help_repo``: the name-only promise is about the
    CACHED path — the one a second and every later ``otto --help`` takes.
    """
    for key in ("version_repo", "help_repo_warm"):
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

    base = harness.surface_by_key("help_repo_warm")
    small = dataclasses.replace(base, key="help_small", sut_files=50, sut_dirs_count=5)
    large = dataclasses.replace(base, key="help_large", sut_files=200, sut_dirs_count=20)

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

    import subprocess
    import sys

    warm = redirect_bytecode_cache(harness.surface_by_key("help_repo_warm"))
    first = harness.measure_surface(warm)["file_ops"]
    # The perturbation: a process filling the bytecode cache mid-flight, by
    # importing what root help imports with bytecode writing on.
    writer_env = dict(harness._sanitized_env(), PYTHONPYCACHEPREFIX=str(tmp_path / "bytecode"))
    writer_env.pop("PYTHONDONTWRITEBYTECODE", None)
    subprocess.run(
        [sys.executable, "-c", "import otto.cli.main, rich.markdown"],
        env=writer_env,
        check=True,
    )
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
    cold = harness.measure_surface(harness.surface_by_key("help_repo"))["file_ops"]
    assert first["workspace"] < cold["workspace"], (
        f"the seed run left nothing behind: warm {first} vs cold {cold}"
    )


def test_surface_by_key_refuses_an_unknown_key():
    """Never index SURFACES positionally — the lookup must be by name."""
    with pytest.raises(KeyError):
        harness.surface_by_key("no_such_surface")


def test_hyperfine_is_gone():
    """Wall-clock never gated and nothing ran it; the tool and its plumbing are removed."""
    from tests._fixtures.paths import PROJECT_ROOT

    hits = []
    for rel in ["Makefile", "scripts", "docs/contributing.md", "docs/architecture", "tests/unit"]:
        root = PROJECT_ROOT / rel
        files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        for f in files:
            if f.suffix == ".pyc" or f.name == "test_import_budget.py":
                continue
            try:
                if "hyperfine" in f.read_text(errors="ignore").lower():
                    hits.append(str(f.relative_to(PROJECT_ROOT)))
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
        f"run `make import-snapshot` under this interpreter"
    )
    extra = sorted(set(recorded) - gated)
    assert not extra, f"baselines for surfaces that are not gated: {extra}"


def test_the_child_runs_in_a_private_empty_cwd(tmp_path, monkeypatch):
    """The child's cwd is the harness's own empty directory, never the caller's.

    ``python -c`` puts the cwd on ``sys.path``, so whatever directory the child
    starts in is one it imports from. Inheriting the caller's cwd hands it the
    repo root, which every other test process in the run writes into.
    """
    import json

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
    mid-measurement: ``help_repo_warm`` on CPython 3.14 read one more than
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

    surface = harness.surface_by_key("help_repo_warm")
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


def test_bootstrap_repo_is_the_repo_bearing_sibling():
    """The pair's whole value is that one of them still sees NO repo.

    ``run_bootstrapped`` isolates the composition root's own import graph, and
    can only do that against an empty workspace: the moment it grows a repo,
    the difference between the two stops being "what a workspace costs at
    bootstrap" and the older surface's count silently starts including it.

    The repo is witnessed by the workspace I/O bootstrap still pays (reading
    the repo's settings, putting its lib dirs on the path, importing its init
    tree); no test file is imported outside a pytest session.
    """
    empty = harness.measure_surface(harness.surface_by_key("run_bootstrapped"))["file_ops"]
    assert empty["workspace"] == 0, empty

    with_repo = harness.measure_surface(harness.surface_by_key("bootstrap_repo"))["file_ops"]
    assert with_repo["workspace"] > 0, with_repo


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
    surface = harness.surface_by_key("completion_repo_warm")
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

    base = harness.surface_by_key("completion_repo_warm")
    small = dataclasses.replace(base, key="completion_small", sut_files=50, sut_dirs_count=5)
    large = dataclasses.replace(base, key="completion_large", sut_files=200, sut_dirs_count=20)

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

    ``completion_repo_warm``'s sibling test above pins the shim's own answer
    path, which reads one JSON file and cannot witness a corpus-scaling walk
    by construction. ``completion_repo_handover`` is the other side of the
    same TAB: a `live` site the shim hands over on, which falls through to
    the unchanged full CLI path — the one that resolves ``tunnel remove``'s
    own module tree and DOES walk the generated repo to discover it. A
    corpus-size regression on that walk is exactly the kind of thing the
    shim's cheapness on the warm path would otherwise let slip past
    unnoticed, so it needs its own pin.
    """
    import dataclasses

    base = harness.surface_by_key("completion_repo_handover")
    small = dataclasses.replace(
        base, key="completion_handover_small", sut_files=50, sut_dirs_count=5
    )
    large = dataclasses.replace(
        base, key="completion_handover_large", sut_files=200, sut_dirs_count=20
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

    base = harness.surface_by_key("dispatch_repo_warm")
    small = dataclasses.replace(base, key="dispatch_small", sut_files=50, sut_dirs_count=5)
    large = dataclasses.replace(base, key="dispatch_large", sut_files=200, sut_dirs_count=20)

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

    Cold ``help_repo`` rebuilds the ``names`` and ``shim`` sections, whose key
    sets are the settings, the init trees and the lab files. Test names come
    only from pytest's collections (the per-file table), so a rebuild neither
    walks nor stats a test file: between 50 files/5 dirs and 200 files/20
    dirs its workspace I/O holds, with the same tolerance as the warm pins.
    """
    import dataclasses

    base = harness.surface_by_key("help_repo")
    small = dataclasses.replace(base, key="rebuild_small", sut_files=50, sut_dirs_count=5)
    large = dataclasses.replace(base, key="rebuild_large", sut_files=200, sut_dirs_count=20)
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

    base = harness.surface_by_key("completion_repo_warm")
    tab = (("_OTTO_COMPLETE", "complete_bash"), ("COMP_WORDS", "otto test T"), ("COMP_CWORD", "2"))
    small = dataclasses.replace(
        base, key="tests_tab_small", sut_files=50, sut_dirs_count=5, env_extra=tab
    )
    large = dataclasses.replace(
        base, key="tests_tab_large", sut_files=200, sut_dirs_count=20, env_extra=tab
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
