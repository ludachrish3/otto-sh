"""Each init area scaffolds artifacts that otto's real ingestion accepts.

Every test drives :func:`otto.init.scaffold` (and the selection functions
beside it) directly; how ``otto init`` prompts for and renders a run is
pinned in ``tests/unit/cli/``.
"""

import json
from pathlib import Path

import pytest

from otto.init import (
    AREA_NAMES,
    FileWrite,
    InitConfig,
    InitInputError,
    ScaffoldReport,
    check_project,
    detect_areas,
    scaffold,
    scaffold_candidates,
    scaffold_prerequisites,
)
from tests._fixtures.project_scaffold import make_project


def _scaffold(root: Path, *areas: str, name: str = "widget") -> ScaffoldReport:
    return scaffold(InitConfig(root, name, "0.1.0"), list(areas))


def _paths(report: ScaffoldReport, outcome: str = "created") -> list[Path]:
    return [w.path for w in report.writes if w.outcome == outcome]


def test_area_order_is_settings_first() -> None:
    assert AREA_NAMES == [
        "settings",
        "schemas",
        "lab",
        "tests",
        "instructions",
        "kmodcov",
        "embedded-gcov",
    ]


def test_settings_scaffold_parses_via_settings_model(tmp_path: Path) -> None:
    created = _paths(_scaffold(tmp_path, "settings"))
    settings = tmp_path / ".otto" / "settings.toml"
    assert settings in created
    import tomli

    from otto.models.settings import SettingsModel

    data = tomli.loads(settings.read_text())
    # adapt: match how Project parses
    model = SettingsModel.model_validate(data)
    assert model.name == "widget"
    # conventional paths pre-wired so later area scaffolds never edit settings
    assert data["lab"]["sources"] == [{"backend": "json", "paths": ["lab_data"]}]
    assert data["tests"] == ["tests"]
    assert data["libs"] == ["pylib"]
    assert data["init"] == ["widget_instructions"]


def test_settings_scaffold_has_commented_monitor_tls_block(tmp_path: Path) -> None:
    """The commented example sections include a `[monitor]` sibling to `[docker]`.

    Pins the raw template text alongside the two TLS keys
    `MonitorSettingsSpec` accepts, so a user uncommenting the block gets a
    working starting point. Commented-out TOML uses the no-space `#key`
    convention, so the template drift test, which uncomments every line, also
    validates the block against the real model.
    """
    _scaffold(tmp_path, "settings")
    text = (tmp_path / ".otto" / "settings.toml").read_text()
    assert "#[monitor]" in text
    assert '#tls_cert = "~/.otto/tls/monitor-cert.pem"' in text
    assert '#tls_key = "~/.otto/tls/monitor-key.pem"' in text


def test_settings_scaffold_has_commented_dependencies_block(tmp_path: Path) -> None:
    """The commented example sections include a `[dependencies]` block.

    Pins the raw template text alongside the two keys `DependenciesSpec`
    accepts, so a user uncommenting the block gets a working starting point.
    Commented-out TOML uses the no-space `#key` convention, so the template
    drift test, which uncomments every line, also validates the block against
    the real model.
    """
    _scaffold(tmp_path, "settings")
    text = (tmp_path / ".otto" / "settings.toml").read_text()
    assert "#[dependencies]" in text
    assert '#required = ["other-project >= 1.0"]' in text
    assert '#optional = ["nice-to-have-project"]' in text


def test_lab_scaffold_writes_three_files_that_resolve_to_one_host(tmp_path: Path) -> None:
    """Spec 2026-09-06 §8.1: lab.json references; inventory.json/creds.json answer under one key."""
    from otto.host.factory import validate_host_dict
    from otto.host.specs import UnixHostSpec
    from otto.init.doctor import _inventory_for
    from otto.inventory import resolve_host_entry
    from otto.models.lab import ElementSpec

    _scaffold(tmp_path, "settings")
    created = _paths(_scaffold(tmp_path, "lab"))
    lab_dir = tmp_path / "lab_data"
    assert set(created) == {
        lab_dir / "lab.json",
        lab_dir / "inventory.json",
        lab_dir / "creds.json",
        lab_dir / "README.md",
    }
    data = json.loads((lab_dir / "lab.json").read_text())
    assert data["links"] == []
    assert data["labs"] == {"example_lab": {"resources": ["example-device"]}}
    element = ElementSpec.model_validate(data["elements"][0])
    assert element.name == "example-device"
    host = element.hosts[0]
    assert host["inventory"] == "device-01.lab.example"
    assert "ip" not in host  # comes from inventory.json
    assert "creds" not in host  # comes from creds.json
    inventory = _inventory_for(tmp_path)
    assert inventory is not None
    resolved = resolve_host_entry(host, inventory, element.to_element()).host_data
    validate_host_dict(resolved, element=element.to_element())
    spec = UnixHostSpec.model_validate(resolved)
    assert spec.ip == "192.0.2.1"
    assert [(c.login, c.password) for c in spec.creds] == [("admin", "CHANGE_ME")]
    assert data["$schema"] == "../.otto/schemas/lab.schema.json"
    assert json.loads((lab_dir / "inventory.json").read_text())["$schema"] == (
        "../.otto/schemas/inventory.schema.json"
    )
    assert json.loads((lab_dir / "creds.json").read_text())["$schema"] == (
        "../.otto/schemas/creds.schema.json"
    )


def test_creds_json_is_written_owner_only_and_nothing_is_overwritten(tmp_path: Path) -> None:
    import stat

    _scaffold(tmp_path, "settings")
    _scaffold(tmp_path, "lab")
    creds = tmp_path / "lab_data" / "creds.json"
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    creds.write_text('{"mine": []}')
    inventory = tmp_path / "lab_data" / "inventory.json"
    inventory.write_text("{}")
    (tmp_path / "lab_data" / "lab.json").unlink()  # area missing again → scaffold runs
    report = _scaffold(tmp_path, "lab")
    assert _paths(report) == [tmp_path / "lab_data" / "lab.json"]
    assert creds in _paths(report, "kept")
    assert inventory in _paths(report, "kept")
    assert creds.read_text() == '{"mine": []}'
    assert inventory.read_text() == "{}"


def test_tests_scaffold_is_plain_pytest(tmp_path: Path) -> None:
    """The scaffold models the shapes the docs teach: a plain class that reads
    the project's options through `ctx.options`, a module logger, a classmethod
    class fixture, a test that uses `expect` and `test_dir`, a plain function —
    and none of the removed spellings."""
    _scaffold(tmp_path, "tests")
    src = (tmp_path / "tests" / "test_example.py").read_text()
    assert "class TestExample:" in src
    assert "ctx.options(ProjectOptions)" in src
    assert "from widget_instructions import ProjectOptions" in src
    assert "logger = logging.getLogger(__name__)" in src
    assert '@pytest.fixture(scope="class", autouse=True)\n    @classmethod' in src
    assert "def test_example_function" in src
    assert "expect(" in src
    assert "test_dir" in src
    for old in (
        "OttoSuite",
        "suite_options",
        "Options = ",
        "self.logger",
        "self.expect",
        "testDir",
        "register_suite",
        "ensure_installed",
        "of old",
    ):
        assert old not in src, old
    conftest = (tmp_path / "tests" / "conftest.py").read_text()
    assert '@pytest_asyncio.fixture(scope="class")' in conftest
    assert 'loop_scope="session"' not in conftest  # the session loop is the default
    # The narrower-pin rule, stated where it will be copied: one shared host
    # instance stays on the session loop, fixtures carry the tests' pin, and a
    # wider-scoped fixture pinned to the class loop is pytest-asyncio's
    # ScopeMismatch.
    assert '@pytest_asyncio.fixture(scope="class", loop_scope="class")' in conftest
    assert "one shared instance per host" in conftest
    assert "ScopeMismatch" in conftest
    assert "opens its own host" not in conftest


def test_instructions_scaffold_imports(tmp_path: Path) -> None:
    _scaffold(tmp_path, "instructions")
    assert (tmp_path / "pylib" / "widget_instructions" / "__init__.py").exists()


@pytest.mark.parametrize("area", AREA_NAMES)
def test_detect_flips_after_scaffold(tmp_path: Path, area: str) -> None:
    """Each area, scaffolded alone into an empty project, is detected afterwards."""
    assert area not in detect_areas(tmp_path)
    _scaffold(tmp_path, area)
    assert area in detect_areas(tmp_path)


def test_the_tests_scaffold_writes_no_options_module(tmp_path: Path) -> None:
    """The example tests import ``ProjectOptions`` from the init module; no other module has it."""
    _scaffold(tmp_path, "settings")
    created = _paths(_scaffold(tmp_path, "tests"))
    assert created == [tmp_path / "tests" / "test_example.py", tmp_path / "tests" / "conftest.py"]
    assert not (tmp_path / "pylib" / "widget_options.py").exists()


def test_the_init_module_declares_and_registers_project_options(tmp_path: Path) -> None:
    """``ProjectOptions`` is declared in the init module, registered by its own decorator."""
    created = _paths(_scaffold(tmp_path, "instructions"))
    init_file = tmp_path / "pylib" / "widget_instructions" / "__init__.py"
    assert [p for p in created if p.is_relative_to(tmp_path / "pylib")] == [init_file]
    assert not (tmp_path / "pylib" / "widget_options.py").exists()
    src = init_file.read_text()
    assert '@otto.options(verbs=["run", "test"])\nclass ProjectOptions:' in src
    assert "register_options" not in src
    assert "hello from widget" in src
    # The instruction receives the class by injection rather than inheriting it.
    assert "@instruction()\nasync def smoke(\n    opts: ProjectOptions," in src
    # The decorator rejects a sync handler, so scaffolding one would make
    # `otto init` emit a project that cannot import.
    assert "async def smoke" in src


def test_importing_the_init_module_registers_project_options_for_both_verbs(
    tmp_path: Path, monkeypatch
) -> None:
    """Importing the scaffolded init module is what registers ``ProjectOptions``.

    The root conftest's registry isolation drops the registration (and the
    ``smoke`` instruction) when the test ends.
    """
    import importlib.util
    import sys

    from otto.params import verbs_for

    _scaffold(tmp_path, "instructions")
    init_file = tmp_path / "pylib" / "widget_instructions" / "__init__.py"
    spec = importlib.util.spec_from_file_location("widget_instructions", init_file)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "widget_instructions", module)
    spec.loader.exec_module(module)
    assert verbs_for(module.ProjectOptions) == ["run", "test"]
    assert module.ProjectOptions().message == "hello from widget"


def test_module_names_are_sanitized_identifiers(tmp_path: Path) -> None:
    cfg = InitConfig(tmp_path, "my-project 2.0", "0.1.0")
    assert cfg.module_base == "my_project_2_0"
    scaffold(cfg, ["settings", "instructions"])
    import tomli

    data = tomli.loads((tmp_path / ".otto" / "settings.toml").read_text())
    assert data["name"] == "my-project 2.0"  # display name keeps the raw value
    assert data["init"] == ["my_project_2_0_instructions"]
    assert (tmp_path / "pylib" / "my_project_2_0_instructions" / "__init__.py").exists()


def test_schemas_scaffold_writes_schema_files(tmp_path: Path) -> None:
    created = _paths(_scaffold(tmp_path, "schemas"))
    out = tmp_path / ".otto" / "schemas"
    for stem in ("settings", "lab", "link", "reservations", "inventory", "creds"):
        assert out / f"{stem}.schema.json" in created
    data = json.loads((out / "lab.schema.json").read_text())
    assert data["title"] == "otto lab.json"


def test_schemas_scaffold_writes_vscode_wiring_when_absent(tmp_path: Path) -> None:
    created = _paths(_scaffold(tmp_path, "schemas"))
    settings = tmp_path / ".vscode" / "settings.json"
    extensions = tmp_path / ".vscode" / "extensions.json"
    assert settings in created
    assert extensions in created
    wiring = json.loads(settings.read_text())
    urls = [entry["url"] for entry in wiring["json.schemas"]]
    assert "./.otto/schemas/lab.schema.json" in urls
    assert "./.otto/schemas/reservations.schema.json" in urls
    assert "./.otto/schemas/inventory.schema.json" in urls
    assert "./.otto/schemas/creds.schema.json" in urls
    matches = {entry["url"]: entry["fileMatch"] for entry in wiring["json.schemas"]}
    assert matches["./.otto/schemas/inventory.schema.json"] == ["**/inventory*.json"]
    assert matches["./.otto/schemas/creds.schema.json"] == ["**/creds*.json"]
    assert "evenBetterToml.schema.associations" in wiring
    toml_associations = wiring["evenBetterToml.schema.associations"]
    assert toml_associations[r".*/settings\.toml$"] == "./.otto/schemas/settings.schema.json"
    assert "tamasfe.even-better-toml" in json.loads(extensions.read_text())["recommendations"]


def test_schemas_scaffold_writes_generated_snippets(tmp_path: Path) -> None:
    """`.vscode/otto.code-snippets` rides with the schemas and is generated."""
    created = _paths(_scaffold(tmp_path, "schemas"))
    snippets = tmp_path / ".vscode" / "otto.code-snippets"
    assert snippets in created
    doc = json.loads(snippets.read_text())
    assert doc["otto unix host"]["prefix"] == "otto-unix-host"
    assert doc["otto element"]["prefix"] == "otto-element"


def test_snippets_are_refreshed_not_preserved(tmp_path: Path) -> None:
    """Unlike the user-owned .vscode/settings.json, a generated file is rewritten.

    The snippets are a product of the live models, so a stale copy from an
    older otto must not survive a refresh — the same rule the schemas follow.
    """
    _scaffold(tmp_path, "schemas")
    snippets = tmp_path / ".vscode" / "otto.code-snippets"
    snippets.write_text("{}")  # simulate a stale file from an older otto
    report = _scaffold(tmp_path, "schemas")
    assert snippets in _paths(report, "refreshed")
    assert json.loads(snippets.read_text()) != {}


def test_snippets_file_is_not_a_validated_artifact(tmp_path: Path) -> None:
    """The doctor must not fail a project over an editor convenience file."""
    _scaffold(tmp_path, "schemas")
    (tmp_path / ".vscode" / "otto.code-snippets").write_text("{not json")
    assert check_project(tmp_path).verdict("schemas").state == "ok"


def test_existing_vscode_settings_left_byte_for_byte_untouched(tmp_path: Path) -> None:
    vscode = tmp_path / ".vscode"
    vscode.mkdir()
    original = '// user file with comments\n{ "editor.rulers": [88] }\n'  # JSONC on purpose
    (vscode / "settings.json").write_text(original)
    report = _scaffold(tmp_path, "schemas")
    assert (vscode / "settings.json").read_text() == original
    assert vscode / "settings.json" in _paths(report, "kept")
    assert vscode / "extensions.json" in _paths(report)  # independent only-if-absent check


def _anchoring_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A project whose path lists mix relative, ``~``-rooted and absolute entries."""
    project = tmp_path / "proj"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    make_project(
        project,
        name="test",
        tests=["tests"],
        # [[lab.sources]] is a table-array header: every top-level key must
        # precede it or it swallows them as source kwargs.
        extra=(
            'libs = ["pylib"]\n'
            "\n"
            "[[lab.sources]]\n"
            'backend = "json"\n'
            'paths = ["lab_data", "~/custom_labs", "/abs/global.json"]\n'
        ),
    )
    return project, home


def test_settings_paths_anchors_relative_and_tilde_paths(tmp_path: Path, monkeypatch) -> None:
    """settings_paths anchors bare relative paths to root and expands ~ to home."""
    from otto.init.settings_file import settings_paths

    project, _home = _anchoring_project(tmp_path, monkeypatch)

    paths = settings_paths(project)
    assert paths is not None

    # Bare relative paths should anchor to project root
    assert paths["tests"][0] == project / "tests"
    assert paths["libs"][0] == project / "pylib"
    # Host data is NOT one of these lists — it is read through lab_files.
    assert set(paths) == {"tests", "libs"}


def test_lab_files_anchor_relative_expand_tilde_and_pass_absolutes_through(
    tmp_path: Path, monkeypatch
) -> None:
    """The lab files init checks come from [[lab.sources]], anchored like every path.

    Same three cases the runtime compiler pins (project-relative, ``~``-rooted,
    absolute), asserted through init's own reader so the doctor can never
    drift from what otto would actually load. Each file is CREATED because
    ``lab_files()`` lists the files a source reads: since ``paths`` entries may
    be globs (spec §2.4), an entry resolving to no file contributes nothing.
    """
    from otto.init.areas import lab_files

    project = tmp_path / "proj"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    absolute = tmp_path / "elsewhere" / "global.json"
    make_project(
        project,
        name="test",
        tests=["tests"],
        extra=(
            'libs = ["pylib"]\n'
            "\n"
            "[[lab.sources]]\n"
            'backend = "json"\n'
            f'paths = ["lab_data", "~/custom_labs", "{absolute}"]\n'
        ),
    )
    for lab_file in (
        project / "lab_data" / "lab.json",
        home / "custom_labs" / "lab.json",
        absolute,
    ):
        lab_file.parent.mkdir(parents=True, exist_ok=True)
        lab_file.write_text("{}")

    assert lab_files(project) == [
        project / "lab_data" / "lab.json",  # relative -> anchored to the project root
        home / "custom_labs" / "lab.json",  # ~ -> home, never the project
        absolute,  # absolute .json entry IS the lab file
    ]


def test_lab_files_falls_back_to_convention_without_settings(tmp_path: Path) -> None:
    """A project otto has not scaffolded yet still gets checked at lab_data/lab.json."""
    from otto.init.areas import lab_files

    assert lab_files(tmp_path) == [tmp_path / "lab_data" / "lab.json"]


def test_lab_files_empty_when_settings_declare_no_lab_table(tmp_path: Path) -> None:
    """Settings that declare no [lab] declare no host data — no conventional guess."""
    from otto.init.areas import lab_files

    project = tmp_path / "proj"
    make_project(project, name="test", tests=["tests"])
    (project / "lab_data").mkdir()
    (project / "lab_data" / "lab.json").write_text("{}")

    assert lab_files(project) == []


def test_area_order_ends_with_the_opt_in_areas() -> None:
    """kmodcov, then embedded-gcov, come last; that they are opt-in is pinned by the all-areas
    candidate test."""
    assert AREA_NAMES[-2:] == ["kmodcov", "embedded-gcov"]


def test_kmodcov_scaffold_exports_the_library_and_appends_the_wiring_entry(
    tmp_path: Path,
) -> None:
    from otto import kmodcov

    _scaffold(tmp_path, "settings")
    report = _scaffold(tmp_path, "kmodcov")
    vendored = tmp_path / "third_party" / "otto_kmodcov"
    assert kmodcov.check_tree(vendored).state == "current"
    assert vendored / "kmodcov.h" in _paths(report)
    settings = (tmp_path / ".otto" / "settings.toml").read_text()
    assert '#kind = "kmodcov"' in settings
    assert '#source = "third_party/otto_kmodcov"' in settings
    assert tmp_path / ".otto" / "settings.toml" in _paths(report, "appended")
    starter = tmp_path / "third_party" / "otto_kmodcov-consumer"
    assert (starter / "kmodcov_begin.c").read_text().strip().endswith("KMODCOV_SENTINEL_BEGIN;")
    assert (starter / "kmodcov_end.c").read_text().strip().endswith("KMODCOV_SENTINEL_END;")
    assert "include $(KMODCOV)/consumer.mk" in (starter / "Kbuild.example").read_text()
    assert "kernel-modules" in (starter / "README.md").read_text()
    assert "kmodcov" in detect_areas(tmp_path)


def test_kmodcov_scaffold_refreshes_the_library_but_never_the_starter_or_a_second_entry(
    tmp_path: Path,
) -> None:
    from otto import kmodcov

    _scaffold(tmp_path, "settings")
    _scaffold(tmp_path, "kmodcov")
    vendored = tmp_path / "third_party" / "otto_kmodcov"
    (vendored / "kmodcov.c").write_text("// edited\n")
    starter = tmp_path / "third_party" / "otto_kmodcov-consumer" / "README.md"
    starter.write_text("mine\n")
    before = (tmp_path / ".otto" / "settings.toml").read_text()
    report = _scaffold(tmp_path, "kmodcov")
    assert kmodcov.check_tree(vendored).state == "current"
    assert vendored / "kmodcov.c" in _paths(report, "refreshed")
    assert starter.read_text() == "mine\n"
    assert starter in _paths(report, "kept")
    assert (tmp_path / ".otto" / "settings.toml").read_text() == before


def test_kmodcov_scaffold_honours_the_configured_directory(tmp_path: Path) -> None:
    cfg = InitConfig(tmp_path, "widget", "0.1.0", kmodcov_dir="vendor/kmodcov")
    scaffold(cfg, ["settings"])
    scaffold(cfg, ["kmodcov"])
    assert (tmp_path / "vendor" / "kmodcov" / "kmodcov.h").is_file()
    assert '#source = "vendor/kmodcov"' in (tmp_path / ".otto" / "settings.toml").read_text()


def test_kmodcov_detects_a_declared_entry_without_the_default_directory(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    make_project(bare, name="widget", version="0.1.0")
    assert "kmodcov" not in detect_areas(bare)
    declared = tmp_path / "declared"
    make_project(
        declared,
        name="widget",
        version="0.1.0",
        extra=(
            '[[dev_tools]]\nname = "kmodcov-6.8"\nkind = "kmodcov"\n'
            'artifact = "build/otto_kmodcov.ko"\nsource = "vendor/kmodcov"\nmatch = { id = ".*" }\n'
        ),
    )
    assert "kmodcov" in detect_areas(declared)


def test_existing_tests_files_are_kept_byte_identical(tmp_path: Path) -> None:
    _scaffold(tmp_path, "settings")
    tests = tmp_path / "tests"
    (tests / "conftest.py").write_bytes(b"# mine\n")
    (tests / "test_example.py").write_bytes(b"def test_mine(): pass\n")
    report = _scaffold(tmp_path, "tests")
    assert {w.path.name: w.outcome for w in report.writes if w.path.parent == tests} == {
        "test_example.py": "kept",
        "conftest.py": "kept",
    }
    assert (tests / "conftest.py").read_bytes() == b"# mine\n"


def test_kmodcov_appends_the_documented_entry_to_existing_settings_once(tmp_path: Path) -> None:
    """``otto init --kmodcov`` on a project that already has settings (#675).

    The kernel-modules page says it appends a commented ``[[dev_tools]]``
    entry: the existing text is kept byte for byte, the template block is
    added after it, and nothing is printed to paste. A second run finds the
    block and appends nothing.
    """
    from otto.init.templates import KMODCOV_DEV_TOOL_TEMPLATE

    _scaffold(tmp_path, "settings")
    settings = tmp_path / ".otto" / "settings.toml"
    before = settings.read_text()
    report = _scaffold(tmp_path, "kmodcov")
    after = settings.read_text()
    assert after.startswith(before)
    block = KMODCOV_DEV_TOOL_TEMPLATE.format(kmodcov_dir="third_party/otto_kmodcov")
    assert after[len(before) :].strip("\n") == block.strip("\n")
    assert not [n for n in report.notices if "[[dev_tools]]" in n]
    # The appended block is TOML comments only: the settings still parse unchanged.
    import tomli

    assert tomli.loads(after) == tomli.loads(before)

    again = _scaffold(tmp_path, "kmodcov")
    assert settings.read_text() == after
    assert settings not in _paths(again, "appended")


def test_kmodcov_appends_after_a_settings_file_with_no_trailing_newline(tmp_path: Path) -> None:
    _scaffold(tmp_path, "settings")
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(settings.read_text().rstrip("\n"))  # scaffold-exempt: in-place edit
    _scaffold(tmp_path, "kmodcov")
    text = settings.read_text()
    assert "\n#[[dev_tools]]\n" in text
    import tomli

    tomli.loads(text)


def test_kmodcov_never_writes_through_a_symlinked_settings_file(tmp_path: Path) -> None:
    _scaffold(tmp_path, "settings")
    settings = tmp_path / ".otto" / "settings.toml"
    real = tmp_path / "real-settings.toml"
    settings.rename(real)
    settings.symlink_to(real)
    before = real.read_bytes()
    report = _scaffold(tmp_path, "kmodcov")
    assert real.read_bytes() == before
    assert settings in _paths(report, "kept")
    (notice,) = [n for n in report.notices if "[[dev_tools]]" in n]
    assert '#kind = "kmodcov"' in notice


def test_kmodcov_leaves_settings_alone_when_a_kmodcov_tool_is_declared(tmp_path: Path) -> None:
    root = tmp_path / "declared"
    make_project(
        root,
        name="widget",
        version="0.1.0",
        extra=(
            '[[dev_tools]]\nname = "kmodcov-6.8"\nkind = "kmodcov"\n'
            'artifact = "build/otto_kmodcov.ko"\nsource = "third_party/otto_kmodcov"\n'
            'match = { id = ".*" }\n'
        ),
    )
    settings = root / ".otto" / "settings.toml"
    before = settings.read_bytes()
    report = _scaffold(root, "kmodcov")
    assert settings.read_bytes() == before
    assert settings not in _paths(report, "appended")


def test_fresh_settings_scaffolded_with_kmodcov_carry_the_block_and_no_notice(
    tmp_path: Path,
) -> None:
    report = _scaffold(tmp_path, "kmodcov")
    assert "settings" in report.prerequisites
    assert '#kind = "kmodcov"' in (tmp_path / ".otto" / "settings.toml").read_text()
    assert not [n for n in report.notices if "[[dev_tools]]" in n]
    # The settings area wrote the block itself: the area must not append it a second time.
    assert tmp_path / ".otto" / "settings.toml" not in _paths(report, "appended")


def test_scaffolding_never_prints(tmp_path: Path, capsys) -> None:
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "settings.json").write_text("{}")
    scaffold(InitConfig(tmp_path, "widget", "0.1.0"), [*AREA_NAMES])
    assert capsys.readouterr() == ("", "")


def test_all_writes_no_duplicate_beside_a_single_file_init_module(tmp_path: Path) -> None:
    make_project(
        tmp_path,
        name="widget",
        tests=["tests"],
        extra='libs = ["pylib"]\ninit = ["foo"]',
        files={"pylib/foo.py": ""},
    )
    assert "instructions" not in scaffold_candidates(tmp_path, all_areas=True)
    scaffold(InitConfig(tmp_path, "widget", "0.1.0"), scaffold_candidates(tmp_path, all_areas=True))
    assert sorted(p.name for p in (tmp_path / "pylib").iterdir()) == ["foo.py"]


@pytest.mark.parametrize("init_line", ["", "init = []"])
def test_all_does_not_offer_instructions_when_no_init_is_declared(
    tmp_path: Path, init_line: str
) -> None:
    make_project(tmp_path, name="widget", tests=["tests"], extra=init_line)
    assert "instructions" not in scaffold_candidates(tmp_path, all_areas=True)
    assert check_project(tmp_path).verdict("instructions").state == "absent"


def test_a_fresh_project_offers_instructions(tmp_path: Path) -> None:
    assert "instructions" in scaffold_candidates(tmp_path, all_areas=True)


def test_an_explicit_instructions_request_with_no_init_writes_and_says_to_declare_it(
    tmp_path: Path,
) -> None:
    make_project(tmp_path, name="widget", tests=["tests"], extra='libs = ["pylib"]')
    report = _scaffold(tmp_path, "instructions")
    assert (tmp_path / "pylib" / "widget_instructions" / "__init__.py").is_file()
    assert any('init = ["widget_instructions"]' in n for n in report.notices)


def test_tests_pulling_instructions_with_no_init_says_to_declare_it(tmp_path: Path) -> None:
    make_project(tmp_path, name="widget", tests=["tests"], extra='libs = ["pylib"]')
    report = _scaffold(tmp_path, "tests")
    assert report.prerequisites == ["instructions"]
    assert any('init = ["widget_instructions"]' in n for n in report.notices)


def test_no_declare_notice_when_the_settings_declare_the_module(tmp_path: Path) -> None:
    report = _scaffold(tmp_path, "tests")  # fresh: the settings template declares the module
    assert not any("init = [" in n for n in report.notices)


def test_a_declared_unresolved_init_scaffolds_that_module_under_the_first_lib(
    tmp_path: Path,
) -> None:
    make_project(
        tmp_path,
        name="widget",
        tests=["tests"],
        extra='libs = ["src", "pylib"]\ninit = ["acme.hooks"]',
    )
    report = _scaffold(tmp_path, "instructions")
    assert (tmp_path / "src" / "acme" / "__init__.py").is_file()
    assert "ProjectOptions" in (tmp_path / "src" / "acme" / "hooks" / "__init__.py").read_text()
    assert not (tmp_path / "pylib" / "widget_instructions").exists()
    assert not any("init = [" in n for n in report.notices)


def test_a_declared_init_that_is_no_module_name_is_never_written_as_a_path(tmp_path: Path) -> None:
    """An ``init`` entry is a module name, never a path: joined as one it could leave the
    project."""
    outside = tmp_path / "outside"
    project = tmp_path / "proj"
    make_project(project, name="widget", extra=f'libs = ["pylib"]\ninit = ["{outside}"]')
    report = _scaffold(project, "instructions")
    assert not outside.exists()
    assert all(w.path.is_relative_to(project) for w in report.writes)
    assert (project / "pylib" / "widget_instructions" / "__init__.py").is_file()


def test_settings_is_a_prerequisite_of_every_area(tmp_path: Path) -> None:
    for area in ("schemas", "lab", "tests", "instructions", "kmodcov", "embedded-gcov"):
        assert scaffold_prerequisites(tmp_path, [area])[0] == "settings", area
    assert scaffold_prerequisites(tmp_path, []) == []


def test_writing_settings_pulls_in_the_init_module_it_declares(tmp_path: Path) -> None:
    """The template declares ``init = ["widget_instructions"]``: without it the project
    cannot load."""
    assert scaffold_prerequisites(tmp_path, ["lab"]) == ["settings", "instructions"]
    assert scaffold_prerequisites(tmp_path, ["settings"]) == ["instructions"]
    _scaffold(tmp_path, "lab")
    assert (tmp_path / "pylib" / "widget_instructions" / "__init__.py").is_file()
    assert check_project(tmp_path).ok


def test_existing_settings_pull_in_nothing_for_a_lab(tmp_path: Path) -> None:
    make_project(tmp_path, name="widget", tests=["tests"], extra="init = []")
    assert scaffold_prerequisites(tmp_path, ["lab"]) == []


def test_the_project_marker_notice_names_a_settings_prerequisite_only(tmp_path: Path) -> None:
    pulled, asked = tmp_path / "pulled", tmp_path / "asked"
    pulled.mkdir()
    asked.mkdir()
    assert any("project marker" in n for n in _scaffold(pulled, "lab").notices)
    assert not any("project marker" in n for n in _scaffold(asked, "settings", "lab").notices)


def test_an_unknown_area_is_refused_naming_areas(tmp_path: Path) -> None:
    with pytest.raises(InitInputError) as caught:
        scaffold_candidates(tmp_path, requested=["labs"])
    assert caught.value.field == "areas"
    with pytest.raises(InitInputError):
        _scaffold(tmp_path, "labs")


def test_the_otto_owned_areas_refresh_when_present_others_do_not(tmp_path: Path) -> None:
    _scaffold(tmp_path, *AREA_NAMES)
    requested = ["schemas", "kmodcov", "embedded-gcov", "lab", "tests"]
    assert scaffold_candidates(tmp_path, requested=requested) == [
        "schemas",
        "kmodcov",
        "embedded-gcov",
    ]


@pytest.mark.parametrize("area", ["kmodcov", "embedded-gcov"])
def test_an_opt_in_area_is_never_an_all_areas_candidate(tmp_path: Path, area: str) -> None:
    assert area not in scaffold_candidates(tmp_path, all_areas=True)


def test_an_orphan_schema_is_pruned_and_reported(tmp_path: Path) -> None:
    _scaffold(tmp_path, "schemas")
    orphan = tmp_path / ".otto" / "schemas" / "retired-host.schema.json"
    orphan.write_text("{}")
    report = _scaffold(tmp_path, "schemas")
    assert FileWrite(orphan, "pruned") in report.writes
    assert check_project(tmp_path).verdict("schemas").state == "ok"


def test_the_existing_vscode_settings_notice(tmp_path: Path) -> None:
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "settings.json").write_text("{}")
    report = _scaffold(tmp_path, "schemas")
    assert any("/cli/schema/editors.html#vs-code" in n for n in report.notices)


def test_scaffolding_instructions_beside_a_resolving_init_module_writes_nothing(
    tmp_path: Path,
) -> None:
    """A direct request must not shadow the user's single-file module with a package."""
    from otto.config.project import find_init_module

    make_project(
        tmp_path,
        name="widget",
        extra='libs = ["pylib"]\ninit = ["widget_hooks_xyz"]',
        files={"pylib/widget_hooks_xyz.py": ""},
    )
    module = tmp_path / "pylib" / "widget_hooks_xyz.py"
    report = _scaffold(tmp_path, "instructions")
    assert sorted(p.name for p in (tmp_path / "pylib").iterdir()) == ["widget_hooks_xyz.py"]
    assert report.writes == [FileWrite(module, "kept")]
    spec = find_init_module("widget_hooks_xyz", [tmp_path / "pylib"])
    assert spec is not None
    assert spec.origin == str(module)


def test_the_undeclared_init_module_goes_under_the_declared_libs(tmp_path: Path) -> None:
    """Following the notice yields a project whose instructions area the doctor passes."""
    make_project(tmp_path, name="widget", extra='libs = ["src"]')
    report = _scaffold(tmp_path, "instructions")
    assert (tmp_path / "src" / "widget_instructions" / "__init__.py").is_file()
    assert not (tmp_path / "pylib" / "widget_instructions").exists()
    (notice,) = [n for n in report.notices if "init = [" in n]
    assert "libs" not in notice
    settings = tmp_path / ".otto" / "settings.toml"
    with settings.open("a") as f:  # scaffold-exempt: following the scaffold's notice
        f.write('init = ["widget_instructions"]\n')
    assert check_project(tmp_path).verdict("instructions").state == "ok"


def test_with_no_libs_the_notice_also_names_the_libs_line(tmp_path: Path) -> None:
    make_project(tmp_path, name="widget")
    report = _scaffold(tmp_path, "instructions")
    assert (tmp_path / "pylib" / "widget_instructions" / "__init__.py").is_file()
    (notice,) = [n for n in report.notices if "init = [" in n]
    assert 'init = ["widget_instructions"]' in notice
    assert 'libs = ["pylib"]' in notice
    settings = tmp_path / ".otto" / "settings.toml"
    with settings.open("a") as f:  # scaffold-exempt: following the scaffold's notice
        f.write('init = ["widget_instructions"]\nlibs = ["pylib"]\n')
    assert check_project(tmp_path).verdict("instructions").state == "ok"


def test_the_example_tests_never_import_from_an_init_entry_that_is_no_module_name(
    tmp_path: Path,
) -> None:
    make_project(tmp_path, name="widget", extra='libs = ["pylib"]\ninit = ["bad-name"]')
    _scaffold(tmp_path, "tests")
    src = (tmp_path / "tests" / "test_example.py").read_text()
    assert "from widget_instructions import ProjectOptions" in src
    assert "bad-name" not in src


def test_a_declared_kmodcov_entry_needs_no_snippet_notice(tmp_path: Path) -> None:
    make_project(
        tmp_path,
        name="widget",
        extra=(
            '[[dev_tools]]\nname = "kmodcov-6.8"\nkind = "kmodcov"\n'
            'artifact = "build/otto_kmodcov.ko"\nsource = "third_party/otto_kmodcov"\n'
            'match = { id = ".*" }\n'
        ),
    )
    report = _scaffold(tmp_path, "kmodcov")
    assert not [n for n in report.notices if "[[dev_tools]]" in n]


def test_instructions_pulled_in_by_new_settings_say_why(tmp_path: Path) -> None:
    report = _scaffold(tmp_path, "lab")
    assert report.prerequisites == ["settings", "instructions"]
    assert any("init module the new settings.toml declares" in n for n in report.notices)
    assert not any("instructions area is a prerequisite" in n for n in report.notices)


def test_instructions_pulled_in_by_tests_keep_the_prerequisite_wording(tmp_path: Path) -> None:
    report = _scaffold(tmp_path, "tests")
    assert any("instructions area is a prerequisite" in n for n in report.notices)


def test_the_scaffolded_test_uses_the_fixture_the_scaffolded_conftest_defines() -> None:
    """Red before the fix: both still said the old fixture name. A rename of one
    without the other would scaffold a project whose own tests error."""
    from otto.init.templates import CONFTEST_TEMPLATE, TEST_EXAMPLE_TEMPLATE

    assert "def project_marker() -> str:" in CONFTEST_TEMPLATE
    assert "project_marker: str" in TEST_EXAMPLE_TEMPLATE
    assert "repo_marker" not in CONFTEST_TEMPLATE + TEST_EXAMPLE_TEMPLATE


# ── embedded-gcov ──────────────────────────────────────────────────────────────

_EGCOV = Path("third_party") / "embedded-gcov-patch"


def test_embedded_gcov_scaffold_exports_the_patch_and_writes_the_starter(tmp_path: Path) -> None:
    from otto.coverage.embedded_gcov.library import LIBRARY_DIR, STARTER_FILES, check_patch

    _scaffold(tmp_path, "settings")
    report = _scaffold(tmp_path, "embedded-gcov")
    vendored = tmp_path / _EGCOV
    assert check_patch(vendored).state == "current"
    assert vendored / "embedded-gcov-zephyr-gcc12plus.patch" in _paths(report)
    starter = tmp_path / "third_party" / "embedded-gcov-patch-consumer"
    assert (starter / "cov_entry.c").read_bytes() == (LIBRARY_DIR / STARTER_FILES[0]).read_bytes()
    assert "instrumenting/embedded.html" in (starter / "README.md").read_text()
    settings = (tmp_path / ".otto" / "settings.toml").read_text()
    assert "#[coverage.embedded]" in settings
    assert tmp_path / ".otto" / "settings.toml" in _paths(report, "appended")
    assert "embedded-gcov" in detect_areas(tmp_path)


def test_embedded_gcov_scaffold_refreshes_the_patch_but_never_the_starter_or_a_second_block(
    tmp_path: Path,
) -> None:
    from otto.coverage.embedded_gcov.library import check_patch

    _scaffold(tmp_path, "settings")
    _scaffold(tmp_path, "embedded-gcov")
    vendored = tmp_path / _EGCOV
    patch_file = vendored / "embedded-gcov-zephyr-gcc12plus.patch"
    patch_file.write_text("edited\n")
    starter = tmp_path / "third_party" / "embedded-gcov-patch-consumer" / "cov_entry.c"
    starter.write_text("// mine\n")
    before = (tmp_path / ".otto" / "settings.toml").read_text()
    report = _scaffold(tmp_path, "embedded-gcov")
    assert check_patch(vendored).state == "current"
    assert patch_file in _paths(report, "refreshed")
    assert starter.read_text() == "// mine\n"
    assert starter in _paths(report, "kept")
    assert (tmp_path / ".otto" / "settings.toml").read_text() == before


def test_embedded_gcov_scaffold_honours_the_configured_directory(tmp_path: Path) -> None:
    cfg = InitConfig(tmp_path, "widget", "0.1.0", embedded_gcov_dir="vendor/egcov")
    scaffold(cfg, ["settings"])
    scaffold(cfg, ["embedded-gcov"])
    assert (tmp_path / "vendor" / "egcov" / "embedded-gcov-zephyr-gcc12plus.patch").is_file()
    assert (tmp_path / "vendor" / "egcov-consumer" / "cov_entry.c").is_file()
    assert "vendor/egcov" in (tmp_path / ".otto" / "settings.toml").read_text()


def test_embedded_gcov_is_detected_only_by_the_patch_under_the_default_directory(
    tmp_path: Path,
) -> None:
    bare = tmp_path / "bare"
    make_project(bare, name="widget", version="0.1.0")
    assert "embedded-gcov" not in detect_areas(bare)
    custom = tmp_path / "custom"
    make_project(custom, name="widget", version="0.1.0")
    scaffold(
        InitConfig(custom, "widget", "0.1.0", embedded_gcov_dir="vendor/egcov"), ["embedded-gcov"]
    )
    assert "embedded-gcov" not in detect_areas(custom)
    default = tmp_path / "default"
    make_project(default, name="widget", version="0.1.0")
    _scaffold(default, "embedded-gcov")
    assert "embedded-gcov" in detect_areas(default)


def test_kmodcov_is_detected_by_the_library_header_under_its_default_directory(
    tmp_path: Path,
) -> None:
    from otto.init.config import DEFAULT_KMODCOV_DIR

    make_project(tmp_path, name="widget", version="0.1.0")
    library = tmp_path / DEFAULT_KMODCOV_DIR
    library.mkdir(parents=True)
    (library / "kmodcov.h").write_text("")
    assert "kmodcov" in detect_areas(tmp_path)


def test_embedded_gcov_appends_the_documented_block_to_existing_settings_once(
    tmp_path: Path,
) -> None:
    from otto.init.templates import EMBEDDED_GCOV_SETTINGS_TEMPLATE

    _scaffold(tmp_path, "settings")
    settings = tmp_path / ".otto" / "settings.toml"
    before = settings.read_text()
    report = _scaffold(tmp_path, "embedded-gcov")
    after = settings.read_text()
    assert after.startswith(before)
    block = EMBEDDED_GCOV_SETTINGS_TEMPLATE.format(
        embedded_gcov_dir="third_party/embedded-gcov-patch"
    )
    assert after[len(before) :].strip("\n") == block.strip("\n")
    assert not [n for n in report.notices if "[coverage.embedded]" in n]
    import tomli

    assert tomli.loads(after) == tomli.loads(before)
    again = _scaffold(tmp_path, "embedded-gcov")
    assert settings.read_text() == after
    assert settings not in _paths(again, "appended")


def test_embedded_gcov_never_writes_through_a_symlinked_settings_file(tmp_path: Path) -> None:
    _scaffold(tmp_path, "settings")
    settings = tmp_path / ".otto" / "settings.toml"
    real = tmp_path / "real-settings.toml"
    settings.rename(real)
    settings.symlink_to(real)
    before = real.read_bytes()
    report = _scaffold(tmp_path, "embedded-gcov")
    assert real.read_bytes() == before
    assert settings in _paths(report, "kept")
    (notice,) = [n for n in report.notices if "[coverage.embedded]" in n]
    assert "#[coverage.embedded]" in notice


def test_embedded_gcov_leaves_settings_alone_when_a_coverage_embedded_table_is_declared(
    tmp_path: Path,
) -> None:
    root = tmp_path / "declared"
    make_project(
        root,
        name="widget",
        version="0.1.0",
        extra='[coverage.embedded]\nbuild_dir = "build"\n',
    )
    settings = root / ".otto" / "settings.toml"
    before = settings.read_bytes()
    report = _scaffold(root, "embedded-gcov")
    assert settings.read_bytes() == before
    assert settings not in _paths(report, "appended")


def test_fresh_settings_scaffolded_with_both_areas_carry_both_blocks_once_and_no_notice(
    tmp_path: Path,
) -> None:
    report = _scaffold(tmp_path, "kmodcov", "embedded-gcov")
    text = (tmp_path / ".otto" / "settings.toml").read_text()
    assert text.count('#kind = "kmodcov"') == 1
    assert text.count("#[coverage.embedded]") == 1
    assert not [n for n in report.notices if "[[dev_tools]]" in n or "[coverage.embedded]" in n]
    assert (tmp_path / "third_party" / "otto_kmodcov" / "kmodcov.h").is_file()
    assert (tmp_path / _EGCOV / "embedded-gcov-zephyr-gcc12plus.patch").is_file()
    # The settings area wrote the block itself: the area must not append it a second time.
    assert tmp_path / ".otto" / "settings.toml" not in _paths(report, "appended")


def test_existing_settings_get_both_blocks_when_both_areas_run_together(tmp_path: Path) -> None:
    _scaffold(tmp_path, "settings")
    _scaffold(tmp_path, "kmodcov", "embedded-gcov")
    text = (tmp_path / ".otto" / "settings.toml").read_text()
    assert text.count('#kind = "kmodcov"') == 1
    assert text.count("#[coverage.embedded]") == 1


def test_fresh_settings_scaffolded_with_embedded_gcov_carry_the_block_and_no_notice(
    tmp_path: Path,
) -> None:
    report = _scaffold(tmp_path, "embedded-gcov")
    assert "settings" in report.prerequisites
    text = (tmp_path / ".otto" / "settings.toml").read_text()
    assert text.count("#[coverage.embedded]") == 1
    assert not [n for n in report.notices if "[coverage.embedded]" in n]
    # The settings area wrote the block itself: the area must not append it a second time.
    assert tmp_path / ".otto" / "settings.toml" not in _paths(report, "appended")


def test_a_commented_block_with_a_space_after_the_hash_counts_as_wired(tmp_path: Path) -> None:
    _scaffold(tmp_path, "settings")
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # scaffold-exempt: in-place edit
        settings.read_text() + "\n# [coverage.embedded]\n# build_dir = 'b'\n"
    )
    before = settings.read_text()
    report = _scaffold(tmp_path, "embedded-gcov")
    assert settings.read_text() == before
    assert settings not in _paths(report, "appended")


def test_an_unparseable_settings_file_declaring_the_table_is_left_alone(tmp_path: Path) -> None:
    _scaffold(tmp_path, "settings")
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # scaffold-exempt: in-place corruption
        settings.read_text() + "\n[coverage.embedded]\nbuild_dir = \n"
    )
    before = settings.read_text()
    _scaffold(tmp_path, "embedded-gcov")
    assert settings.read_text() == before


# ── the shared vendored-area core ──────────────────────────────────────────────

_VENDORED_AREAS = {
    "kmodcov": ("kmodcov_dir", "vendor/kmod", '#kind = "kmodcov"'),
    "embedded-gcov": ("embedded_gcov_dir", "vendor/egcov", "#[coverage.embedded]"),
}


@pytest.mark.parametrize("area", list(_VENDORED_AREAS))
def test_both_vendored_areas_run_through_one_core_with_the_same_input(
    tmp_path: Path, area: str
) -> None:
    """The same input drives both areas: outcomes, consumer-dir naming, append once, paste."""
    field, directory, marker = _VENDORED_AREAS[area]
    cfg = InitConfig(tmp_path, "widget", "0.1.0", **{field: directory})
    scaffold(cfg, ["settings"])
    settings = tmp_path / ".otto" / "settings.toml"
    first = scaffold(cfg, [area])
    created = [w for w in first.writes if w.path.parent == tmp_path / directory]
    assert created
    assert {w.outcome for w in created} == {"created"}
    consumer = tmp_path / f"{directory}-consumer"
    assert {w.outcome for w in first.writes if w.path.parent == consumer} == {"created"}
    assert settings in _paths(first, "appended")
    assert settings.read_text().count(marker) == 1
    again = scaffold(cfg, [area])
    assert {w.outcome for w in again.writes if w.path.parent == tmp_path / directory} == {
        "refreshed"
    }
    assert {w.outcome for w in again.writes if w.path.parent == consumer} == {"kept"}
    assert settings not in _paths(again, "appended")
    assert settings.read_text().count(marker) == 1


@pytest.mark.parametrize("area", list(_VENDORED_AREAS))
def test_both_vendored_areas_paste_instead_of_writing_through_a_symlink(
    tmp_path: Path, area: str
) -> None:
    _, _, marker = _VENDORED_AREAS[area]
    _scaffold(tmp_path, "settings")
    settings = tmp_path / ".otto" / "settings.toml"
    real = tmp_path / "real-settings.toml"
    settings.rename(real)
    settings.symlink_to(real)
    before = real.read_bytes()
    report = _scaffold(tmp_path, area)
    assert real.read_bytes() == before
    (notice,) = [n for n in report.notices if marker in n]
    assert notice.startswith("add this commented")


def test_an_unparseable_settings_file_declaring_a_kmodcov_entry_is_left_alone(
    tmp_path: Path,
) -> None:
    _scaffold(tmp_path, "settings")
    settings = tmp_path / ".otto" / "settings.toml"
    settings.write_text(  # scaffold-exempt: in-place corruption
        settings.read_text() + '\n[[dev_tools]]\nkind = "kmodcov"\nartifact = \n'
    )
    before = settings.read_text()
    _scaffold(tmp_path, "kmodcov")
    assert settings.read_text() == before
