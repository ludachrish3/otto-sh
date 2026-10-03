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
    check_repo,
    detect_areas,
    scaffold,
    scaffold_candidates,
    scaffold_prerequisites,
)
from tests._fixtures.sutrepo import make_sut_repo


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
    ]


def test_settings_scaffold_parses_via_settings_model(tmp_path: Path) -> None:
    created = _paths(_scaffold(tmp_path, "settings"))
    settings = tmp_path / ".otto" / "settings.toml"
    assert settings in created
    import tomli

    from otto.models.settings import SettingsModel

    data = tomli.loads(settings.read_text())
    model = SettingsModel.model_validate(data)  # adapt: match how Repo parses (see repo.py:532-561)
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
    from otto.init.doctor import _inventory_for
    from otto.inventory import resolve_host_entry
    from otto.models.host import UnixHostSpec
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
    validate_host_dict(resolved)
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
    the repo's options through `ctx.options`, a module logger, a classmethod
    class fixture, a test that uses `expect` and `test_dir`, a plain function —
    and none of the removed spellings."""
    _scaffold(tmp_path, "tests")
    src = (tmp_path / "tests" / "test_example.py").read_text()
    assert "class TestExample:" in src
    assert "ctx.options(RepoOptions)" in src
    assert "from widget_instructions import RepoOptions" in src
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
    """Each area, scaffolded alone into an empty repo, is detected afterwards."""
    assert area not in detect_areas(tmp_path)
    _scaffold(tmp_path, area)
    assert area in detect_areas(tmp_path)


def test_the_tests_scaffold_writes_no_options_module(tmp_path: Path) -> None:
    """The example tests import ``RepoOptions`` from the init module; no other module holds it."""
    _scaffold(tmp_path, "settings")
    created = _paths(_scaffold(tmp_path, "tests"))
    assert created == [tmp_path / "tests" / "test_example.py", tmp_path / "tests" / "conftest.py"]
    assert not (tmp_path / "pylib" / "widget_options.py").exists()


def test_the_init_module_declares_and_registers_repo_options(tmp_path: Path) -> None:
    """``RepoOptions`` is declared in the init module, registered by its own decorator."""
    created = _paths(_scaffold(tmp_path, "instructions"))
    init_file = tmp_path / "pylib" / "widget_instructions" / "__init__.py"
    assert [p for p in created if p.is_relative_to(tmp_path / "pylib")] == [init_file]
    assert not (tmp_path / "pylib" / "widget_options.py").exists()
    src = init_file.read_text()
    assert '@otto.options(verbs=["run", "test"])\nclass RepoOptions:' in src
    assert "register_options" not in src
    assert "hello from widget" in src
    # The instruction receives the class by injection rather than inheriting it.
    assert "@instruction()\nasync def smoke(\n    opts: RepoOptions," in src
    # The decorator rejects a sync handler, so scaffolding one would make
    # `otto init` emit a repo that cannot import.
    assert "async def smoke" in src


def test_importing_the_init_module_registers_repo_options_for_both_verbs(
    tmp_path: Path, monkeypatch
) -> None:
    """Importing the scaffolded init module is what registers ``RepoOptions``.

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
    assert verbs_for(module.RepoOptions) == ["run", "test"]
    assert module.RepoOptions().message == "hello from widget"


def test_module_names_are_sanitized_identifiers(tmp_path: Path) -> None:
    cfg = InitConfig(tmp_path, "my-repo 2.0", "0.1.0")
    assert cfg.module_base == "my_repo_2_0"
    scaffold(cfg, ["settings", "instructions"])
    import tomli

    data = tomli.loads((tmp_path / ".otto" / "settings.toml").read_text())
    assert data["name"] == "my-repo 2.0"  # display name keeps the raw value
    assert data["init"] == ["my_repo_2_0_instructions"]
    assert (tmp_path / "pylib" / "my_repo_2_0_instructions" / "__init__.py").exists()


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
    """The doctor must not fail a repo over an editor convenience file."""
    _scaffold(tmp_path, "schemas")
    (tmp_path / ".vscode" / "otto.code-snippets").write_text("{not json")
    assert check_repo(tmp_path).verdict("schemas").state == "ok"


def test_existing_vscode_settings_left_byte_for_byte_untouched(tmp_path: Path) -> None:
    vscode = tmp_path / ".vscode"
    vscode.mkdir()
    original = '// user file with comments\n{ "editor.rulers": [88] }\n'  # JSONC on purpose
    (vscode / "settings.json").write_text(original)
    report = _scaffold(tmp_path, "schemas")
    assert (vscode / "settings.json").read_text() == original
    assert vscode / "settings.json" in _paths(report, "kept")
    assert vscode / "extensions.json" in _paths(report)  # independent only-if-absent check


def _anchoring_repo(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A SUT repo whose path lists mix relative, ``~``-rooted and absolute entries."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    make_sut_repo(
        repo,
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
    return repo, home


def test_settings_paths_anchors_relative_and_tilde_paths(tmp_path: Path, monkeypatch) -> None:
    """settings_paths anchors bare relative paths to root and expands ~ to home."""
    from otto.init.settings_file import settings_paths

    repo, _home = _anchoring_repo(tmp_path, monkeypatch)

    paths = settings_paths(repo)
    assert paths is not None

    # Bare relative paths should anchor to repo root
    assert paths["tests"][0] == repo / "tests"
    assert paths["libs"][0] == repo / "pylib"
    # Host data is NOT one of these lists — it is read through lab_files.
    assert set(paths) == {"tests", "libs"}


def test_lab_files_anchor_relative_expand_tilde_and_pass_absolutes_through(
    tmp_path: Path, monkeypatch
) -> None:
    """The lab files init checks come from [[lab.sources]], anchored like every path.

    Same three cases the runtime compiler pins (repo-relative, ``~``-rooted,
    absolute), asserted through init's own reader so the doctor can never
    drift from what otto would actually load. Each file is CREATED because
    ``lab_files()`` lists the files a source reads: since ``paths`` entries may
    be globs (spec §2.4), an entry resolving to no file contributes nothing.
    """
    from otto.init.areas import lab_files

    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    absolute = tmp_path / "elsewhere" / "global.json"
    make_sut_repo(
        repo,
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
    for lab_file in (repo / "lab_data" / "lab.json", home / "custom_labs" / "lab.json", absolute):
        lab_file.parent.mkdir(parents=True, exist_ok=True)
        lab_file.write_text("{}")

    assert lab_files(repo) == [
        repo / "lab_data" / "lab.json",  # relative -> anchored to the repo root
        home / "custom_labs" / "lab.json",  # ~ -> home, never the repo
        absolute,  # absolute .json entry IS the lab file
    ]


def test_lab_files_falls_back_to_convention_without_settings(tmp_path: Path) -> None:
    """A repo otto has not scaffolded yet still gets checked at lab_data/lab.json."""
    from otto.init.areas import lab_files

    assert lab_files(tmp_path) == [tmp_path / "lab_data" / "lab.json"]


def test_lab_files_empty_when_settings_declare_no_lab_table(tmp_path: Path) -> None:
    """Settings that declare no [lab] declare no host data — no conventional guess."""
    from otto.init.areas import lab_files

    repo = tmp_path / "repo"
    make_sut_repo(repo, name="test", tests=["tests"])
    (repo / "lab_data").mkdir()
    (repo / "lab_data" / "lab.json").write_text("{}")

    assert lab_files(repo) == []


def test_area_order_ends_with_the_opt_in_kmodcov_area() -> None:
    """kmodcov comes last; that it is opt-in is pinned by the all-areas candidate test."""
    assert AREA_NAMES[-1] == "kmodcov"


def test_kmodcov_scaffold_exports_the_library_and_returns_the_wiring_snippet(
    tmp_path: Path,
) -> None:
    from otto import kmodcov

    _scaffold(tmp_path, "settings")
    report = _scaffold(tmp_path, "kmodcov")
    vendored = tmp_path / "third_party" / "otto_kmodcov"
    assert kmodcov.check_tree(vendored).state == "current"
    assert vendored / "kmodcov.h" in _paths(report)
    (notice,) = [n for n in report.notices if "[[dev_tools]]" in n]
    assert '#kind = "kmodcov"' in notice
    assert '#source = "third_party/otto_kmodcov"' in notice
    assert '#kind = "kmodcov"' not in (tmp_path / ".otto" / "settings.toml").read_text()
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
    report = scaffold(cfg, ["kmodcov"])
    assert (tmp_path / "vendor" / "kmodcov" / "kmodcov.h").is_file()
    assert any('#source = "vendor/kmodcov"' in n for n in report.notices)


def test_kmodcov_detects_a_declared_entry_without_the_default_directory(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    make_sut_repo(bare, name="widget", version="0.1.0")
    assert "kmodcov" not in detect_areas(bare)
    declared = tmp_path / "declared"
    make_sut_repo(
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


def test_kmodcov_never_edits_existing_settings_and_returns_the_snippet(tmp_path: Path) -> None:
    _scaffold(tmp_path, "settings")
    settings = tmp_path / ".otto" / "settings.toml"
    before = settings.read_bytes()
    report = _scaffold(tmp_path, "kmodcov")
    assert settings.read_bytes() == before
    (notice,) = [n for n in report.notices if "[[dev_tools]]" in n]
    assert '#kind = "kmodcov"' in notice
    assert "third_party/otto_kmodcov" in notice


def test_fresh_settings_scaffolded_with_kmodcov_carry_the_block_and_no_notice(
    tmp_path: Path,
) -> None:
    report = _scaffold(tmp_path, "kmodcov")
    assert "settings" in report.prerequisites
    assert '#kind = "kmodcov"' in (tmp_path / ".otto" / "settings.toml").read_text()
    assert not [n for n in report.notices if "[[dev_tools]]" in n]


def test_scaffolding_never_prints(tmp_path: Path, capsys) -> None:
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "settings.json").write_text("{}")
    scaffold(InitConfig(tmp_path, "widget", "0.1.0"), [*AREA_NAMES])
    assert capsys.readouterr() == ("", "")


def test_all_writes_no_duplicate_beside_a_single_file_init_module(tmp_path: Path) -> None:
    make_sut_repo(
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
    make_sut_repo(tmp_path, name="widget", tests=["tests"], extra=init_line)
    assert "instructions" not in scaffold_candidates(tmp_path, all_areas=True)
    assert check_repo(tmp_path).verdict("instructions").state == "absent"


def test_a_fresh_repo_offers_instructions(tmp_path: Path) -> None:
    assert "instructions" in scaffold_candidates(tmp_path, all_areas=True)


def test_an_explicit_instructions_request_with_no_init_writes_and_says_to_declare_it(
    tmp_path: Path,
) -> None:
    make_sut_repo(tmp_path, name="widget", tests=["tests"], extra='libs = ["pylib"]')
    report = _scaffold(tmp_path, "instructions")
    assert (tmp_path / "pylib" / "widget_instructions" / "__init__.py").is_file()
    assert any('init = ["widget_instructions"]' in n for n in report.notices)


def test_tests_pulling_instructions_with_no_init_says_to_declare_it(tmp_path: Path) -> None:
    make_sut_repo(tmp_path, name="widget", tests=["tests"], extra='libs = ["pylib"]')
    report = _scaffold(tmp_path, "tests")
    assert report.prerequisites == ["instructions"]
    assert any('init = ["widget_instructions"]' in n for n in report.notices)


def test_no_declare_notice_when_the_settings_declare_the_module(tmp_path: Path) -> None:
    report = _scaffold(tmp_path, "tests")  # fresh: the settings template declares the module
    assert not any("init = [" in n for n in report.notices)


def test_a_declared_unresolved_init_scaffolds_that_module_under_the_first_lib(
    tmp_path: Path,
) -> None:
    make_sut_repo(
        tmp_path,
        name="widget",
        tests=["tests"],
        extra='libs = ["src", "pylib"]\ninit = ["acme.hooks"]',
    )
    report = _scaffold(tmp_path, "instructions")
    assert (tmp_path / "src" / "acme" / "__init__.py").is_file()
    assert "RepoOptions" in (tmp_path / "src" / "acme" / "hooks" / "__init__.py").read_text()
    assert not (tmp_path / "pylib" / "widget_instructions").exists()
    assert not any("init = [" in n for n in report.notices)


def test_a_declared_init_that_is_no_module_name_is_never_written_as_a_path(tmp_path: Path) -> None:
    """An ``init`` entry is a module name, never a path: joined as one it could leave the repo."""
    outside = tmp_path / "outside"
    repo = tmp_path / "repo"
    make_sut_repo(repo, name="widget", extra=f'libs = ["pylib"]\ninit = ["{outside}"]')
    report = _scaffold(repo, "instructions")
    assert not outside.exists()
    assert all(w.path.is_relative_to(repo) for w in report.writes)
    assert (repo / "pylib" / "widget_instructions" / "__init__.py").is_file()


def test_settings_is_a_prerequisite_of_every_area(tmp_path: Path) -> None:
    for area in ("schemas", "lab", "tests", "instructions", "kmodcov"):
        assert scaffold_prerequisites(tmp_path, [area])[0] == "settings", area
    assert scaffold_prerequisites(tmp_path, []) == []


def test_writing_settings_pulls_in_the_init_module_it_declares(tmp_path: Path) -> None:
    """The template declares ``init = ["widget_instructions"]``: without it the repo cannot load."""
    assert scaffold_prerequisites(tmp_path, ["lab"]) == ["settings", "instructions"]
    assert scaffold_prerequisites(tmp_path, ["settings"]) == ["instructions"]
    _scaffold(tmp_path, "lab")
    assert (tmp_path / "pylib" / "widget_instructions" / "__init__.py").is_file()
    assert check_repo(tmp_path).ok


def test_existing_settings_pull_in_nothing_for_a_lab(tmp_path: Path) -> None:
    make_sut_repo(tmp_path, name="widget", tests=["tests"], extra="init = []")
    assert scaffold_prerequisites(tmp_path, ["lab"]) == []


def test_the_repo_marker_notice_names_a_settings_prerequisite_only(tmp_path: Path) -> None:
    pulled, asked = tmp_path / "pulled", tmp_path / "asked"
    pulled.mkdir()
    asked.mkdir()
    assert any("repo marker" in n for n in _scaffold(pulled, "lab").notices)
    assert not any("repo marker" in n for n in _scaffold(asked, "settings", "lab").notices)


def test_an_unknown_area_is_refused_naming_areas(tmp_path: Path) -> None:
    with pytest.raises(InitInputError) as caught:
        scaffold_candidates(tmp_path, requested=["labs"])
    assert caught.value.field == "areas"
    with pytest.raises(InitInputError):
        _scaffold(tmp_path, "labs")


def test_schemas_and_kmodcov_refresh_when_present_others_do_not(tmp_path: Path) -> None:
    _scaffold(tmp_path, *AREA_NAMES)
    assert scaffold_candidates(tmp_path, requested=["schemas", "kmodcov", "lab", "tests"]) == [
        "schemas",
        "kmodcov",
    ]


def test_kmodcov_is_never_an_all_areas_candidate(tmp_path: Path) -> None:
    assert "kmodcov" not in scaffold_candidates(tmp_path, all_areas=True)


def test_an_orphan_schema_is_pruned_and_reported(tmp_path: Path) -> None:
    _scaffold(tmp_path, "schemas")
    orphan = tmp_path / ".otto" / "schemas" / "retired-host.schema.json"
    orphan.write_text("{}")
    report = _scaffold(tmp_path, "schemas")
    assert FileWrite(orphan, "pruned") in report.writes
    assert check_repo(tmp_path).verdict("schemas").state == "ok"


def test_the_existing_vscode_settings_notice(tmp_path: Path) -> None:
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "settings.json").write_text("{}")
    report = _scaffold(tmp_path, "schemas")
    assert any("docs/cli/schema/editors.md" in n for n in report.notices)


def test_scaffolding_instructions_beside_a_resolving_init_module_writes_nothing(
    tmp_path: Path,
) -> None:
    """A direct request must not shadow the user's single-file module with a package."""
    from otto.config.repo import find_init_module

    make_sut_repo(
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
    """Following the notice yields a repo whose instructions area the doctor passes."""
    make_sut_repo(tmp_path, name="widget", extra='libs = ["src"]')
    report = _scaffold(tmp_path, "instructions")
    assert (tmp_path / "src" / "widget_instructions" / "__init__.py").is_file()
    assert not (tmp_path / "pylib" / "widget_instructions").exists()
    (notice,) = [n for n in report.notices if "init = [" in n]
    assert "libs" not in notice
    settings = tmp_path / ".otto" / "settings.toml"
    with settings.open("a") as f:  # sutrepo-exempt: following the scaffold's notice
        f.write('init = ["widget_instructions"]\n')
    assert check_repo(tmp_path).verdict("instructions").state == "ok"


def test_with_no_libs_the_notice_also_names_the_libs_line(tmp_path: Path) -> None:
    make_sut_repo(tmp_path, name="widget")
    report = _scaffold(tmp_path, "instructions")
    assert (tmp_path / "pylib" / "widget_instructions" / "__init__.py").is_file()
    (notice,) = [n for n in report.notices if "init = [" in n]
    assert 'init = ["widget_instructions"]' in notice
    assert 'libs = ["pylib"]' in notice
    settings = tmp_path / ".otto" / "settings.toml"
    with settings.open("a") as f:  # sutrepo-exempt: following the scaffold's notice
        f.write('init = ["widget_instructions"]\nlibs = ["pylib"]\n')
    assert check_repo(tmp_path).verdict("instructions").state == "ok"


def test_the_example_tests_never_import_from_an_init_entry_that_is_no_module_name(
    tmp_path: Path,
) -> None:
    make_sut_repo(tmp_path, name="widget", extra='libs = ["pylib"]\ninit = ["bad-name"]')
    _scaffold(tmp_path, "tests")
    src = (tmp_path / "tests" / "test_example.py").read_text()
    assert "from widget_instructions import RepoOptions" in src
    assert "bad-name" not in src


def test_a_declared_kmodcov_entry_needs_no_snippet_notice(tmp_path: Path) -> None:
    make_sut_repo(
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
