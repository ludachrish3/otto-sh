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
    expected = {
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
        "completion_repo_warm",
    }
    assert set(keys) == expected


def test_exactly_two_surfaces_cover_the_composition_root():
    """The bootstrap-inclusive surfaces must exist, and the lazy ones stay lazy.

    Kills the blind spot the first of them was added for: every other surface
    resolves a dispatch target WITHOUT calling `bootstrap()`, so bootstrap-time
    imports went unmeasured. Deleting `bootstrap=True` from the table (or
    letting `measure_surface` drop the flag) restores that hole silently — the
    snapshots would simply be regenerated smaller — so the presence of the
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


def test_check_surface_passes_for_real_measurement():
    surface = harness.SURFACES[0]  # import_otto
    result = harness.measure_surface(surface)
    assert harness.check_surface(surface, result) == []


def test_check_surface_flags_cap_violation():
    import dataclasses

    surface = harness.SURFACES[0]
    result = harness.measure_surface(surface)
    # Force the cap below the real count; the snapshot still matches, so only
    # the cap check fires.
    tight = dataclasses.replace(surface, cap=0)
    violations = harness.check_surface(tight, result)
    assert any("non-stdlib modules >" in v for v in violations)


@pytest.mark.parametrize("surface", harness.SURFACES, ids=lambda s: s.key)
def test_import_budget(surface):
    result = harness.measure_surface(surface)
    violations = harness.check_surface(surface, result)
    assert not violations, "\n".join(violations)


def test_measure_reports_io_counts():
    result = harness.measure(["python"])
    io = result["io"]
    assert set(io) == {"open", "scandir", "listdir", "open_fixture", "open_home"}
    assert all(isinstance(v, int) for v in io.values())
    # Importing otto reads files. Zero here means the hook was installed after
    # the work it exists to observe.
    assert io["open"] > 0
    # ...and this child has neither a fixture nor an OTTO_HOME (a bare
    # `measure` call passes the sanitized env, which carries no OTTO_*), so
    # both scoped halves read 0.
    assert io["open_fixture"] == 0
    assert io["open_home"] == 0


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
            "from otto.suite import OttoSuite, OttoOptionsPlugin; print('ok')",
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
    not. Measured on ``bootstrap_repo`` with the pin dropped: the GATED
    ``open_fixture`` reads 10, then 4, then 4 — order-dependent inside one run,
    which is precisely the flake class the whole-process ``open`` count was
    rejected for. Asserted here rather than trusted to a comment.
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
        f"their gated open_fixture count becomes order-dependent"
    )


def test_every_surface_pins_a_private_otto_home():
    """``OTTO_HOME`` is pinned on EVERY surface, not only the repo-bearing ones.

    ``open_home`` is gated, and it attributes by prefix against ``$OTTO_HOME``.
    A surface that does not pin one therefore fails twice over: the child
    resolves ``~/.otto``, so whatever home I/O it performs lands on the
    runner's real home — machine state, and the counter reads whatever that
    box happens to carry — and the prefix tuple is empty, so ``open_home``
    reads 0 no matter what the child does. Either alone disqualifies the
    counter as a gate.

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
    """`otto --version` must not read the repo at all (Task 4's headline).

    Before the console-script shim: 601 opens at 50 test files, growing ONE PER
    TEST FILE — a delta of 150 between a 200-file corpus and a 50-file one.
    After: 73 opens and a delta of 0, because the shim answers `--version` off
    `otto.version` alone and never bootstraps.

    Asserts BOTH forms, and the delta is the load-bearing half. An absolute
    bound alone is satisfied by a broken harness: if OTTO_SUT_DIRS stopped
    reaching the child, or the fixture generation silently produced nothing,
    the count would also be low and this would pass for the wrong reason. The
    delta can only be zero because the corpus size stopped mattering.
    """
    import dataclasses

    base = harness.surface_by_key("version_repo")
    large = dataclasses.replace(base, key="version_repo_large", sut_files=200)
    small_io = harness.measure_surface(base)["io"]
    delta = harness.measure_surface(large)["io"]["open"] - small_io["open"]
    assert delta == 0, f"--version still scales with the corpus: delta {delta}"
    assert small_io["open"] < 100, f"--version still reads the corpus: {small_io}"
    assert small_io["scandir"] == 0, f"--version still walks the repo: {small_io}"


def test_repo_bearing_surfaces_actually_walk_the_generated_repo():
    """``scandir`` is the ONLY observable signal for the stat-only walk.

    There is no ``os.stat`` audit event, and on 3.10 ``pathlib`` binds its stat
    accessor at import time, so an ``os.stat`` monkeypatch counts zero against
    hundreds of real stats. A repo-bearing surface reporting ``scandir == 0``
    is not measuring a repo at all — the env injection failed, and every later
    task's acceptance would be a no-op that passes for the wrong reason.

    SCOPED TO ``help_repo`` SINCE TASK 4. ``version_repo`` legitimately stops
    walking (that is the fix), so keeping it here would fail BECAUSE the fix
    landed. The proof this test carries — that the harness's env injection
    actually reaches a child and finds a real repo — is what must survive.

    ``help_repo`` STILL CARRIES IT AFTER TASK 7, and that is why the surface
    was kept rather than converted to ``warm=True``. Every measurement is
    cold by construction (a fresh ``OTTO_HOME`` per call), and a cold
    ``--help`` MUST take the full load — cache-or-load never degrades the
    screen — so this surface walks the corpus exactly as it always did. The
    fix is observed on ``help_repo_warm``, whose scandir count is 1.
    """
    surface = harness.surface_by_key("help_repo")
    io = harness.measure_surface(surface)["io"]
    # The walk costs two scandirs per subdirectory plus a small constant
    # (13 at dirs=5, 83 at dirs=40). Bounding on `2 * dirs` rather than on
    # `> 0` also catches "found the repo but stopped walking part-way".
    floor = 2 * surface.sut_dirs_count
    assert io["scandir"] >= floor, f"help_repo walked {io['scandir']} < {floor}: {io}"


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
    ``sys.addaudithook`` is stubbed first because audit hooks CANNOT be
    removed once installed, and this exec happens inside the pytest process.
    """
    import sys as _sys

    root = "/synthetic/fixture/root"
    monkeypatch.setenv(harness.FIXTURE_ROOT_ENV_VAR, root)
    monkeypatch.setattr(_sys, "addaudithook", lambda hook: None)
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
    exec(harness._CHILD_IO_PREAMBLE, namespace)  # noqa: S102 — the harness's own source
    assert namespace["_fixture_path_entries"]() == 3

    # And the early-out branch: no fixture root in the env means 0 whatever
    # sys.path holds — which is why the 0s elsewhere cannot prove liveness.
    monkeypatch.delenv(harness.FIXTURE_ROOT_ENV_VAR)
    bare = {}
    exec(harness._CHILD_IO_PREAMBLE, bare)  # noqa: S102 — the harness's own source
    assert bare["_fixture_path_entries"]() == 0


def test_name_only_surfaces_execute_no_suite_modules():
    """Importing a repo's test files to answer --version/--help is the defect.

    ``import_test_file`` names them ``_otto_suite_<stem>``, so their presence in
    ``sys.modules`` is a direct, cheap signal.

    ``help_repo_warm``, not ``help_repo``: a COLD help legitimately executes
    them, because a cold cache means a full load and a full load registers.
    The name-only promise is about the CACHED path — the one a second and
    every later ``otto --help`` takes.
    """
    for key in ("version_repo", "help_repo_warm"):
        mods = harness.measure_surface(harness.surface_by_key(key))["modules"]
        suites = [m for m in mods if m.startswith("_otto_suite_")]
        assert suites == [], f"{key} executed suite modules: {suites}"


def test_help_io_does_not_scale_with_corpus_size():
    """The cached help path must be O(1) in corpus size, not merely capped.

    A flat cap only catches a constant growing. Gates BOTH signals: `open` is
    the per-file read signal, `scandir` the per-directory walk signal. otto
    walks with os.walk, which fires one scandir per directory and no per-file
    event at all — so omitting the scandir delta would leave the fingerprint
    walk invisible.

    Gates the WARM variants. Cold help still walks and still reads per file,
    by design (measured before this task: open 724 -> 874 and scandir 14 -> 44
    between the two sizes); the guarantee this task adds is that the SECOND
    run stops paying for a corpus it never reports on.
    """
    import dataclasses

    base = harness.surface_by_key("help_repo_warm")
    small = dataclasses.replace(base, key="help_small", sut_files=50, sut_dirs_count=5)
    large = dataclasses.replace(base, key="help_large", sut_files=200, sut_dirs_count=20)

    io_small = harness.measure_surface(small)["io"]
    io_large = harness.measure_surface(large)["io"]

    assert io_large["open"] - io_small["open"] <= 5, (
        f"help reads scale with corpus: {io_small['open']} -> {io_large['open']}"
    )
    assert io_large["scandir"] - io_small["scandir"] <= 5, (
        f"help walks scale with corpus: {io_small['scandir']} -> {io_large['scandir']}"
    )


def test_a_warm_surface_is_seeded_and_repeats_identically():
    """``warm=True`` must actually seed, and warm measurement must be repeatable.

    Two properties, and the first is what keeps the second honest. Determinism
    alone cannot detect a broken seed: two COLD measurements agree too (Task 3
    made every measurement independent, and ``PYTHONDONTWRITEBYTECODE`` keeps
    the fixture tree from warming), so ``a == b`` would pass unchanged if
    ``measure_surface`` stopped running the seed altogether. The strict
    inequality against the cold twin is the half that fails when it does.

    The two surfaces have the same shape and therefore byte-identical
    generated corpora; only the seed differs.
    """
    warm = harness.surface_by_key("help_repo_warm")
    first = harness.measure_surface(warm)["io"]
    second = harness.measure_surface(warm)["io"]
    assert first == second, f"repeat warm measurements disagree: {first} vs {second}"

    cold = harness.measure_surface(harness.surface_by_key("help_repo"))["io"]
    assert first["scandir"] < cold["scandir"], (
        f"the seed run left nothing behind: warm {first} vs cold {cold}"
    )


def test_cap_exemption_covers_exactly_the_repo_bearing_surfaces():
    """The ``has no cap set`` exemption must not quietly widen — and must shrink.

    Two sets, deliberately separate. ELIGIBLE is what ``check_surface`` would
    excuse (``real_entry``); ACTUALLY CAPLESS is what still uses the excuse.
    THE SECOND SET IS NOW EMPTY: Task 4 capped ``version_repo`` and Task 7 the
    help pair, so the countdown this test tracked has reached zero. Pinning it
    empty (rather than asserting it equals the first) is what keeps the
    exemption from being re-used silently: a new capless surface, or a deleted
    cap, fails here even though ``check_surface`` would still excuse it.
    """
    import dataclasses

    eligible = {s.key for s in harness.SURFACES if s.real_entry}
    assert eligible == {"version_repo", "help_repo", "help_repo_warm", "completion_repo_warm"}
    capless = {s.key for s in harness.SURFACES if s.cap is None}
    assert capless == set(), f"every repo-bearing surface is capped now; still capless: {capless}"

    # A capless surface OUTSIDE the exemption is still a violation...
    capped = harness.surface_by_key("import_otto")
    result = harness.measure_surface(capped)
    capless = dataclasses.replace(capped, cap=None)
    assert any("has no cap set" in v for v in harness.check_surface(capless, result))
    # ...and one inside it is not.
    exempted = dataclasses.replace(capless, real_entry=True)
    assert not any("has no cap set" in v for v in harness.check_surface(exempted, result))


def test_surface_by_key_refuses_an_unknown_key():
    """Never index SURFACES positionally — the lookup must be by name."""
    with pytest.raises(KeyError):
        harness.surface_by_key("no_such_surface")


# --- I/O goldens (Task 10) --------------------------------------------------
#
# The release gate's instrument. `make profile` no longer runs hyperfine: a
# wall-clock number fails for reasons outside the change (load, thermals, page
# cache) and so can only ever be monitoring. The counters below repeat
# identically and are what actually predicted a real NFS deployment, so they
# are what gates.


def test_every_surface_has_an_io_golden_for_this_interpreter():
    """A missing golden is a NAMED failure — never a silent skip.

    Cheap and direct, ahead of the measuring gate: the goldens are keyed per
    Python minor, so "this interpreter has no file" is the shape a newly added
    interpreter (or a newly added surface) takes, and it must not be possible
    for a leg to report green having compared nothing.
    """
    missing = [s.key for s in harness.SURFACES if not harness.io_snapshot_path(s.key).exists()]
    assert not missing, (
        f"no I/O golden on CPython {harness.interpreter_tag()} for {missing} — "
        f"run `make import-snapshot` under this interpreter"
    )


def test_check_surface_flags_an_io_drift():
    """Mutate one gated counter and the gate must fire, naming the surface.

    The parametrized budget test above passes whether or not `check_surface`
    still looks at I/O at all — deleting the comparison would simply make every
    surface pass. This is the assertion that fails when it is deleted.
    """
    surface = harness.surface_by_key("help_repo_warm")
    result = harness.measure_surface(surface)
    assert harness.check_surface(surface, result) == []

    result["io"]["scandir"] += 1
    violations = harness.check_surface(surface, result)
    assert any("I/O counts changed" in v and "help_repo_warm" in v for v in violations), violations


def test_missing_io_golden_fails_by_name(monkeypatch):
    """The message must carry the file, the interpreter, and the fix.

    Driven through the real interpreter key rather than a fake surface: a
    surface with no module snapshot would fail earlier, on a different check,
    and prove nothing about this one.
    """
    monkeypatch.setattr(harness, "interpreter_tag", lambda: "9.99")
    surface = harness.surface_by_key("version_repo")
    result = harness.measure_surface(surface)
    violations = [v for v in harness.check_surface(surface, result) if "I/O golden" in v]
    assert len(violations) == 1, violations
    message = violations[0]
    assert "version_repo.io.9.99.txt" in message
    assert "CPython 9.99" in message
    assert "make import-snapshot" in message


def test_open_fixture_is_the_gated_half_of_open():
    """The scoped counter must count the workspace, and only the workspace.

    ``open`` itself is deliberately NOT gated: it drifts with bytecode-cache
    state and with how many distributions are installed (measured, and written
    up on ``_CHILD_IO_PREAMBLE``), neither of which has anything to do with
    otto. The scoped half carries the signal a startup-I/O budget is actually
    about, so it has to be observed non-zero somewhere or it is a dead
    instrument that every gate then leans on.
    """
    cold = harness.measure_surface(harness.surface_by_key("help_repo"))["io"]
    assert cold["open_fixture"] > 0, cold
    assert cold["open_fixture"] < cold["open"], cold

    # ...and zero where there is no workspace at all, which is the other end.
    assert harness.measure_surface(harness.surface_by_key("run"))["io"]["open_fixture"] == 0


def test_open_home_is_the_home_side_half_of_open_fixture():
    """``open_home`` must be observed non-zero, and INSIDE the fixture total.

    The goldens pin every surface's exact number, so why this: ``--update``
    re-blesses whatever is measured. A counter that stopped counting reads 0
    everywhere, a regeneration writes those zeros down, and every golden goes
    green on a dead instrument — the same trap
    ``test_open_fixture_is_the_gated_half_of_open`` exists for. A non-zero pin
    is what survives a regeneration.

    The subset relation is the second half, and it is what says the counter is
    pointed at the right root: a repo-bearing surface's ``OTTO_HOME`` lives
    INSIDE its fixture root (``surface_env``), so every home-side open is also
    a fixture open and ``open_home <= open_fixture`` must hold. A counter
    matching some other prefix — the real ``~/.otto``, say — could exceed it.

    THE COLD-VS-WARM COMPARISON IS THE THIRD, and it covers the half the warm
    number cannot see. The home is touched by two different call paths — the
    hit's ``Path.read_text`` (an ``_io.open``) and the miss's atomic write (a
    ``tempfile`` ``os.open``) — so a counter that went dead on the WRITE path
    alone would leave the warm surface reading exactly what it reads now, drop
    the cold one by the write's share, and pass every assertion above once
    ``--update`` re-blessed the new number. Cold must exceed warm because a
    miss pays a write that a hit never does. No magic number: the relation is
    between two measurements, so it survives a legitimate change to either.
    """
    warm = harness.measure_surface(harness.surface_by_key("help_repo_warm"))["io"]
    assert warm["open_home"] > 0, warm
    assert warm["open_home"] <= warm["open_fixture"], warm

    cold = harness.measure_surface(harness.surface_by_key("help_repo"))["io"]
    assert cold["open_home"] > warm["open_home"], (cold, warm)

    # ...and zero where there is no workspace to resolve a home for, which is
    # the other end. The home IS pinned there (every surface pins one), so this
    # zero is measured rather than structural.
    assert harness.measure_surface(harness.surface_by_key("run"))["io"]["open_home"] == 0


def test_bootstrap_repo_is_the_repo_bearing_sibling():
    """The pair's whole value is that one of them still sees NO repo.

    ``run_bootstrapped`` isolates the composition root's own import graph, and
    can only do that against an empty workspace: the moment it grows a repo,
    the difference between the two stops being "what a workspace costs at
    bootstrap" and the older surface's snapshot silently starts including it.
    """
    empty = harness.measure_surface(harness.surface_by_key("run_bootstrapped"))["io"]
    assert empty["scandir"] == 0, empty
    assert empty["open_fixture"] == 0, empty

    with_repo = harness.measure_surface(harness.surface_by_key("bootstrap_repo"))["io"]
    assert with_repo["open_fixture"] > 0, with_repo
    assert with_repo["scandir"] > empty["scandir"], (with_repo, empty)


def test_completion_env_reaches_the_child():
    """``env_extra`` is what makes the completion surface a completion surface.

    Without it the same argv is a bare ``otto``, which renders the root help
    screen — so the surface would keep its name, keep passing, and measure
    something else entirely. The inequality is what fails when the field stops
    reaching the child.

    ON THE MODULE COUNT, NOT ON I/O, and the failure that produced this test
    is the reason: measured, the two agree on every gated I/O counter
    (``scandir`` 1, ``open_fixture`` 2), because bare ``otto`` is served from
    the same cached ``names`` section completion is. What separates them is
    that completion renders NOTHING — no rich markdown, no pygments — which
    the module count sees and the workspace I/O, correctly, does not.
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
    """The steady-state TAB cost must be O(1) in corpus size, not merely capped.

    The same guarantee ``test_help_io_does_not_scale_with_corpus_size`` makes
    for warm root help, for the surface a user hits most often. Both signals,
    for the reason written there: ``open`` is the per-file read and ``scandir``
    the per-directory walk, and otto walks with ``os.walk``, which fires no
    per-file event at all.

    This is also where ``open`` stays gated. The absolute number is not
    comparable across environments, but a DELTA between two measurements taken
    in one environment is — whatever the bytecode cache and the installed dists
    add, they add to both sides.
    """
    import dataclasses

    base = harness.surface_by_key("completion_repo_warm")
    small = dataclasses.replace(base, key="completion_small", sut_files=50, sut_dirs_count=5)
    large = dataclasses.replace(base, key="completion_large", sut_files=200, sut_dirs_count=20)

    io_small = harness.measure_surface(small)["io"]
    io_large = harness.measure_surface(large)["io"]

    assert io_large["open"] - io_small["open"] <= 5, (
        f"completion reads scale with corpus: {io_small['open']} -> {io_large['open']}"
    )
    assert io_large["scandir"] - io_small["scandir"] <= 5, (
        f"completion walks scale with corpus: {io_small['scandir']} -> {io_large['scandir']}"
    )


def test_a_golden_of_the_wrong_shape_diagnoses_itself(tmp_path, monkeypatch):
    """A golden carrying a non-gated key must fail with a REASON, not a blank diff.

    The comparison is over the whole parsed file, so a stale or hand-edited
    golden — a renamed counter, a leftover `open` line — is unequal while every
    gated value still matches. Before this, that produced
    ``golden -> measured: {}``: a red naming nothing. Both directions are
    checked, since a golden can be missing a gated counter as easily as it can
    carry one it should not.
    """
    surface = harness.surface_by_key("version_repo")
    result = harness.measure_surface(surface)
    assert harness.check_surface(surface, result) == []

    monkeypatch.setattr(harness, "SNAPSHOT_DIR", tmp_path)
    real = {name: result["io"][name] for name in harness.GATED_IO_COUNTERS}
    harness.write_snapshot(surface.key, result["otto_modules"])

    # 1. every gated value correct, plus one key that is not gated at all.
    body = "".join(f"{k} {v}\n" for k, v in real.items()) + "open 999\n"
    harness.io_snapshot_path(surface.key).write_text(body)
    violations = harness.check_surface(surface, result)
    assert any(
        "I/O golden mismatch" in v and "NOT gated" in v and "open" in v for v in violations
    ), violations

    # 2. the other direction: a gated counter the file does not carry.
    dropped = dict(real)
    dropped.pop("scandir")
    harness.io_snapshot_path(surface.key).write_text(
        "".join(f"{k} {v}\n" for k, v in dropped.items())
    )
    violations = harness.check_surface(surface, result)
    assert any("absent from the golden" in v and "scandir" in v for v in violations), violations


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
