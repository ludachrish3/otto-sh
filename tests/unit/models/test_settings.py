import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from otto.config.repo import DockerCompose, DockerImage, DockerSettings
from otto.models.settings import (
    DockerComposeSpec,
    DockerImageSpec,
    DockerSettingsSpec,
    LabConfigSpec,
    OsProfileSpec,
    OttoEnvSettings,
    ReservationConfigSpec,
    ReservationEntry,
    ReservationFile,
    SettingsModel,
)

_OTTO_ENV_VARS = (
    "OTTO_SUT_DIRS",
    "OTTO_LAB",
    "OTTO_XDIR",
    "OTTO_COMPOSE_SUFFIX",
    "OTTO_FIELD_DEFAULT",
    "OTTO_FIELD_PRODUCTS",
    "OTTO_LOG_DAYS",
    "OTTO_LOG_LEVEL",
    "OTTO_LOG_RICH",
    "OTTO_TEARDOWN_DEADLINE",
)


@pytest.fixture
def clean_otto_env(monkeypatch):
    """Clear every OTTO_* var so a stray ambient value can't skew the model."""
    for var in _OTTO_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_docker_settings_spec_defaults_to_empty_runtime():
    rt = DockerSettingsSpec().to_runtime()
    assert isinstance(rt, DockerSettings)
    assert rt.registry_url == "docker.io"
    assert rt.images == ()
    assert rt.composes == ()


def test_docker_image_spec_builds_runtime_with_sorted_tupled_build_args():
    spec = DockerSettingsSpec.model_validate(
        {
            "registry_url": "reg.example",
            "images": [
                {
                    "name": "api",
                    "dockerfile": "/repo/docker/Dockerfile",
                    "context": "/repo/docker",
                    "target": "prod",
                    "build_args": {"B": "2", "A": "1"},
                }
            ],
        }
    )
    rt = spec.to_runtime()
    assert isinstance(rt.images[0], DockerImage)
    img = rt.images[0]
    assert img.name == "api"
    assert img.dockerfile == Path("/repo/docker/Dockerfile")
    assert img.context == Path("/repo/docker")
    assert img.target == "prod"
    assert img.build_args == (("A", "1"), ("B", "2"))


def test_docker_compose_spec_builds_runtime():
    spec = DockerSettingsSpec.model_validate(
        {
            "composes": [
                {
                    "path": "/repo/compose.yml",
                    "services": ["api", "worker"],
                }
            ],
        }
    )
    rt = spec.to_runtime()
    assert isinstance(rt.composes[0], DockerCompose)
    assert rt.composes[0].path == Path("/repo/compose.yml")
    assert rt.composes[0].services == ("api", "worker")


def test_docker_image_spec_stringifies_scalar_build_args():
    # parity with the old TOML parser: a bare-scalar build arg (e.g. an int) is
    # accepted and stringified rather than rejected at validation.
    spec = DockerImageSpec.model_validate(
        {
            "name": "api",
            "dockerfile": "/d/Dockerfile",
            "context": "/d",
            "build_args": {"PORT": 8080, "DEBUG": True},
        }
    )
    assert spec.to_runtime().build_args == (("DEBUG", "True"), ("PORT", "8080"))


def test_docker_spec_forbids_unknown_top_level_key():
    with pytest.raises(
        ValidationError, match=r"(?m)^registy_url\n\s+Extra inputs are not permitted"
    ):
        DockerSettingsSpec.model_validate({"registy_url": "x"})  # typo


def test_docker_image_spec_requires_name_dockerfile_context():
    # Two independent searches, not one ordered pattern: a legitimate field
    # reorder in DockerImageSpec must not fail this test.
    with pytest.raises(ValidationError, match=r"(?m)^dockerfile\n\s+Field required") as exc:
        DockerImageSpec.model_validate({"name": "api"})  # missing dockerfile/context
    assert re.search(r"(?m)^context\n\s+Field required", str(exc.value))


# ---------------------------------------------------------------------------
# Task 2: OsProfileSpec, ReservationConfigSpec, ReservationEntry, ReservationFile
# ---------------------------------------------------------------------------


def test_os_profile_spec_requires_base_and_collects_defaults():
    spec = OsProfileSpec.model_validate(
        {
            "base": "embedded",
            "os_name": "Zephyr",
            "os_version": "3.7",
            "command_frame": "zephyr",
            "max_filename_len": 32,
        }
    )
    assert spec.base == "embedded"
    assert spec.defaults == {
        "os_name": "Zephyr",
        "os_version": "3.7",
        "command_frame": "zephyr",
        "max_filename_len": 32,
    }


def test_os_profile_spec_missing_base_raises():
    with pytest.raises(ValidationError, match=r"(?m)^base\n\s+Field required"):
        OsProfileSpec.model_validate({"os_name": "Zephyr"})


def test_os_profile_spec_bare_minimum_defaults_to_empty():
    # `defaults` is the only public surface — confirm it's {} (not None) when
    # the profile declares only `base`.
    assert OsProfileSpec(base="unix").defaults == {}


def test_reservation_config_defaults_to_none_backend():
    cfg = ReservationConfigSpec()
    assert cfg.backend == "none"
    assert cfg.url is None


def test_reservation_config_keeps_open_backend_subtable():
    cfg = ReservationConfigSpec.model_validate(
        {
            "backend": "json",
            "json": {"path": "reservations.json"},
        }
    )
    assert cfg.backend == "json"
    assert cfg.model_extra == {"json": {"path": "reservations.json"}}


def test_reservation_config_rejects_non_string_backend():
    with pytest.raises(ValidationError, match=r"(?m)^backend\n\s+Input should be a valid string"):
        ReservationConfigSpec.model_validate({"backend": 3})


def test_reservation_file_parses_entries_and_z_suffix():
    f = ReservationFile.model_validate(
        {
            "version": 1,
            "reservations": [
                {"user": "alice", "resources": ["rack3-psu"], "expires": "2099-01-01T00:00:00Z"},
                {"user": "bob", "resources": ["rack4-psu"]},
            ],
        }
    )
    assert isinstance(f.reservations[0], ReservationEntry)
    assert f.reservations[0].user == "alice"
    assert f.reservations[0].expires == datetime(2099, 1, 1, tzinfo=timezone.utc)
    assert f.reservations[1].expires is None


def test_reservation_file_naive_expires_treated_as_utc():
    f = ReservationFile.model_validate(
        {
            "version": 1,
            "reservations": [{"user": "a", "resources": ["r"], "expires": "2099-01-01T00:00:00"}],
        }
    )
    assert f.reservations[0].expires == datetime(2099, 1, 1, tzinfo=timezone.utc)


def test_reservation_file_rejects_bad_version():
    with pytest.raises(ValidationError, match=r"(?m)^version\n\s+Input should be 1"):
        ReservationFile.model_validate({"version": 2, "reservations": []})


def test_reservation_file_rejects_malformed_expires():
    # a bad timestamp surfaces as ValidationError (the validator must not swallow
    # the underlying ValueError from datetime.fromisoformat).
    with pytest.raises(
        ValidationError,
        match=r"(?m)^reservations\.0\.expires\n\s+Value error, Invalid isoformat string",
    ):
        ReservationFile.model_validate(
            {
                "version": 1,
                "reservations": [{"user": "a", "resources": ["r"], "expires": "not-a-date"}],
            }
        )


def test_reservation_file_rejects_non_string_resources():
    with pytest.raises(
        ValidationError,
        match=r"(?m)^reservations\.0\.resources\.0\n\s+Input should be a valid string",
    ):
        ReservationFile.model_validate(
            {
                "version": 1,
                "reservations": [{"user": "a", "resources": [3]}],
            }
        )


# ---------------------------------------------------------------------------
# Task 4: SettingsModel
# ---------------------------------------------------------------------------


def _minimal() -> dict:
    return {"name": "repo1", "version": "1.0.0"}


def test_settings_requires_name_and_version():
    with pytest.raises(ValidationError, match=r"(?m)^version\n\s+Field required") as exc:
        SettingsModel.model_validate({"name": "repo1"})  # no version
    assert "version" in str(exc.value)


def test_settings_rejects_bad_version_format():
    with pytest.raises(
        ValidationError,
        match=r"(?m)^version\n\s+Value error, version '1\.0' must be MAJOR\.MINOR\.PATCH",
    ):
        SettingsModel.model_validate({"name": "r", "version": "1.0"})  # not X.Y.Z


def test_settings_version_allows_semver_suffix():
    # Version validation accepts optional extra tags with '-', '+', or '.'
    # separators (consistent with the runtime Version parser).
    m = SettingsModel.model_validate({"name": "r", "version": "1.2.3-rc1"})
    assert m.version == "1.2.3-rc1"


def test_settings_allows_typed_coverage():
    m = SettingsModel.model_validate(
        {
            **_minimal(),
            "coverage": {"gcda_remote_dir": "/var/cov", "embedded": {"extension": "cov"}},
        }
    )
    assert m.coverage.gcda_remote_dir == "/var/cov"
    assert m.coverage.embedded == {"extension": "cov"}


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        # (?s): the `labs` message prints the replacement TOML block, so the
        # key it names and the spelling it points at land on different lines.
        ({"labs": ["lab_data"]}, r"(?s)labs.*\[\[lab\.sources\]\]"),
        ({"lab_data_type": "json"}, r"lab_data_type.*removed"),
        ({"lab": {"backend": "json"}}, r"backend.*\[\[lab\.sources\]\]"),
        ({"lab": {"sources": []}}, r"declares no sources"),
        ({"lab": {}}, r"declares no sources"),
    ],
)
def test_removed_lab_spellings_fail_with_migration_message(extra, match):
    data = {"name": "r", "version": "1.0.0", **extra}
    with pytest.raises((ValidationError, ValueError), match=match):
        SettingsModel.model_validate(data)


def test_lab_subtable_kwargs_are_rejected():
    data = {
        "name": "r",
        "version": "1.0.0",
        "lab": {
            "sources": [{"backend": "json", "paths": ["l"]}],
            "cmdb": {"server": "db"},  # the removed [lab.cmdb] kwarg-table spelling
        },
    }
    with pytest.raises(ValidationError, match="cmdb"):
        SettingsModel.model_validate(data)


def test_settings_forbids_unknown_top_level_key():
    with pytest.raises(
        ValidationError, match=r"(?m)^labz\n\s+Extra inputs are not permitted"
    ) as exc:
        SettingsModel.model_validate({**_minimal(), "labz": []})  # typo: labs
    assert "labz" in str(exc.value)


def test_settings_paths_coerce_to_path_lists():
    m = SettingsModel.model_validate(
        {
            **_minimal(),
            "libs": ["/a/lib"],
            "tests": ["/a/tests"],
            "init": ["mod_a"],
        }
    )
    assert m.libs == [Path("/a/lib")]
    assert m.tests == [Path("/a/tests")]
    assert m.init == ["mod_a"]


def test_host_preferences_accepts_selections_and_option_tables():
    m = SettingsModel.model_validate(
        {
            "name": "p",
            "version": "1.0.0",
            "host_preferences": {
                ".*": {"term": ["telnet"], "ssh_options": {"connect_timeout": 5.0}},
                "router.*": {"telnet_options": {"port": 9023}},
            },
        }
    )
    assert m.host_preferences[".*"]["term"] == ["telnet"]
    assert m.host_preferences[".*"]["ssh_options"] == {"connect_timeout": 5.0}
    assert m.host_preferences["router.*"]["telnet_options"] == {"port": 9023}


def test_host_preferences_unknown_inner_key_raises():
    with pytest.raises(ValueError, match=r"unknown \[host_preferences\] key 'bogus'"):
        SettingsModel.model_validate(
            {
                "name": "p",
                "version": "1.0.0",
                "host_preferences": {".*": {"bogus": ["x"]}},
            }
        )


def test_host_preferences_bad_selector_regex_raises():
    with pytest.raises(ValueError, match="is not a valid regular expression"):
        SettingsModel.model_validate(
            {
                "name": "p",
                "version": "1.0.0",
                "host_preferences": {"[": {"term": ["ssh"]}},
            }
        )


def test_host_preferences_option_table_typo_raises():
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        SettingsModel.model_validate(
            {
                "name": "p",
                "version": "1.0.0",
                "host_preferences": {".*": {"ssh_options": {"not_a_real_key": 1}}},
            }
        )


def test_host_preferences_capability_must_be_list():
    with pytest.raises(ValueError, match="must be a list"):
        SettingsModel.model_validate(
            {
                "name": "p",
                "version": "1.0.0",
                "host_preferences": {".*": {"term": "telnet"}},
            }
        )


def test_host_preferences_accepts_impairer_selection():
    m = SettingsModel.model_validate(
        {
            "name": "p",
            "version": "1.0.0",
            "host_preferences": {".*": {"impairer": ["netem"]}},
        }
    )
    assert m.host_preferences[".*"]["impairer"] == ["netem"]


def test_legacy_host_defaults_rejected_with_migration_message():
    with pytest.raises(ValueError, match=r"\[host_defaults\] was removed"):
        SettingsModel.model_validate(
            {
                "name": "p",
                "version": "1.0.0",
                "host_defaults": {"ssh_options": {"port": 22}},
            }
        )


def test_settings_schema_exposes_host_preferences_not_host_defaults():
    schema = SettingsModel.model_json_schema()
    assert "host_preferences" in schema["properties"]
    assert "host_defaults" not in schema["properties"]


def test_settings_builds_docker_and_os_profiles():
    m = SettingsModel.model_validate(
        {
            **_minimal(),
            "os_profiles": {"zephyr-3.7": {"base": "embedded", "os_version": "3.7"}},
            "docker": {"registry_url": "reg.x"},
        }
    )
    assert m.os_profiles["zephyr-3.7"].base == "embedded"
    assert m.os_profiles["zephyr-3.7"].defaults == {"os_version": "3.7"}
    assert m.docker.to_runtime().registry_url == "reg.x"


def test_settings_validates_every_in_tree_fixture():
    """Every real settings.toml validates — the regression guard for the
    extra='forbid' top-level key set.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # py3.10
        import tomli as tomllib
    for name in ("repo1", "repo2", "repo3"):
        raw = (Path("tests") / name / ".otto" / "settings.toml").read_text()
        SettingsModel.model_validate(tomllib.loads(raw))


def test_settings_host_preferences_accepted():
    m = SettingsModel.model_validate(
        {
            **_minimal(),
            "host_preferences": {
                ".*": {"transfer": ["sftp", "scp"], "term": ["ssh"]},
                "zephyr.*": {"transfer": ["console"]},
            },
        }
    )
    assert m.host_preferences == {
        ".*": {"transfer": ["sftp", "scp"], "term": ["ssh"]},
        "zephyr.*": {"transfer": ["console"]},
    }


def test_settings_host_preferences_defaults_empty():
    m = SettingsModel.model_validate(_minimal())
    assert m.host_preferences == {}


def test_settings_host_preferences_rejects_unknown_capability():
    with pytest.raises(ValueError, match=r"unknown \[host_preferences\] key 'transfre'"):
        SettingsModel.model_validate(
            {
                **_minimal(),
                "host_preferences": {".*": {"transfre": ["scp"]}},
            }
        )


def test_settings_host_preferences_rejects_bad_selector_regex():
    with pytest.raises(ValueError, match=r"not a valid regular expression"):
        SettingsModel.model_validate(
            {
                **_minimal(),
                "host_preferences": {"[unclosed": {"transfer": ["scp"]}},
            }
        )


def test_host_default_option_keys_match_factory_options_keys():
    from otto.host.factory import OPTIONS_KEYS
    from otto.models.settings import _HOST_DEFAULT_OPTION_SPECS

    assert set(_HOST_DEFAULT_OPTION_SPECS) == OPTIONS_KEYS


def test_docker_spec_fields_match_runtime_dataclass():
    """Bidirectional drift guard: each docker spec's field names match its
    runtime dataclass's init fields (so a field added to one but not the other
    is caught), mirroring HOST_SPEC_RUNTIME_PAIRS in test_host_specs.py.
    """
    import dataclasses

    from otto.config.repo import DockerCompose, DockerImage, DockerSettings
    from otto.models.settings import (
        DockerSettingsSpec,
    )

    pairs = [
        (DockerImageSpec, DockerImage),
        (DockerComposeSpec, DockerCompose),
        (DockerSettingsSpec, DockerSettings),
    ]
    for spec_cls, rt_cls in pairs:
        spec_fields = set(spec_cls.model_fields)
        rt_fields = {f.name for f in dataclasses.fields(rt_cls) if f.init}
        assert spec_fields == rt_fields, (
            f"{spec_cls.__name__} <-> {rt_cls.__name__}: "
            f"spec-only={sorted(spec_fields - rt_fields)}, "
            f"runtime-only={sorted(rt_fields - spec_fields)}"
        )


# ---------------------------------------------------------------------------
# OttoEnvSettings — the OTTO_* env surface
# ---------------------------------------------------------------------------


def test_otto_env_settings_defaults(clean_otto_env):
    env = OttoEnvSettings()
    assert env.sut_dirs == []
    assert env.lab is None
    assert env.xdir is None
    assert env.log_days == 30
    assert env.log_level == "INFO"
    assert env.log_rich is False
    assert env.field_default is None
    assert env.field_products is None
    assert env.compose_suffix is None


def test_otto_env_settings_reads_prefixed_vars(clean_otto_env, tmp_path):
    clean_otto_env.setenv("OTTO_SUT_DIRS", str(tmp_path))
    clean_otto_env.setenv("OTTO_COMPOSE_SUFFIX", "ci")
    clean_otto_env.setenv("OTTO_FIELD_DEFAULT", "1")
    env = OttoEnvSettings()
    assert env.sut_dirs == [tmp_path]
    assert env.compose_suffix == "ci"
    assert env.field_default == "1"


def test_otto_env_settings_splits_sut_dirs_comma_and_pathsep(clean_otto_env, tmp_path):
    import os

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    clean_otto_env.setenv("OTTO_SUT_DIRS", f"{a},{b}")
    assert OttoEnvSettings().sut_dirs == [a, b]
    clean_otto_env.setenv("OTTO_SUT_DIRS", f"{a}{os.pathsep}{b}")
    assert OttoEnvSettings().sut_dirs == [a, b]


def test_otto_env_settings_empty_values_use_defaults(clean_otto_env):
    # env_ignore_empty: an empty OTTO_* var means "unset" -> the field default,
    # NOT a parse crash. OTTO_LOG_RICH="" must not break startup.
    clean_otto_env.setenv("OTTO_LOG_RICH", "")
    clean_otto_env.setenv("OTTO_LOG_DAYS", "")
    clean_otto_env.setenv("OTTO_XDIR", "")
    env = OttoEnvSettings()
    assert env.log_rich is False
    assert env.log_days == 30
    assert env.xdir is None  # empty OTTO_XDIR disables the completion cache


def test_otto_env_settings_xdir_dot_is_preserved(clean_otto_env):
    # a real value (even ".") is kept — only the empty string means "unset".
    clean_otto_env.setenv("OTTO_XDIR", ".")
    assert OttoEnvSettings().xdir == Path()


def test_teardown_deadline_reads_env(monkeypatch):
    monkeypatch.setenv("OTTO_TEARDOWN_DEADLINE", "3.5")
    assert OttoEnvSettings().teardown_deadline == 3.5


def test_teardown_deadline_default(clean_otto_env):
    assert OttoEnvSettings().teardown_deadline == 10.0


# ---------------------------------------------------------------------------
# LabConfigSpec + SettingsModel.lab wiring
# ---------------------------------------------------------------------------


def test_lab_source_entry_keeps_backend_kwargs_inline():
    """A custom backend's kwargs ride IN the entry — that is what keeps
    ``[lab.<backend>]`` tables unnecessary (and, above, rejected)."""
    cfg = LabConfigSpec.model_validate({"sources": [{"backend": "myteam", "url": "https://cmdb"}]})
    assert [s.backend for s in cfg.sources] == ["myteam"]
    assert cfg.sources[0].model_extra == {"url": "https://cmdb"}


def test_settings_model_accepts_lab_sources_block():
    m = SettingsModel.model_validate(
        {
            "name": "demo",
            "version": "1.0.0",
            "lab": {"sources": [{"backend": "json", "paths": ["lab_data"]}]},
        }
    )
    assert m.lab is not None
    assert [s.backend for s in m.lab.sources] == ["json"]


def test_settings_model_lab_is_none_when_absent():
    """No ``[lab]`` table means no host sources at all — not a defaulted one."""
    m = SettingsModel.model_validate({"name": "demo", "version": "1.0.0"})
    assert m.lab is None


class TestMonitorSettings:
    """The [monitor] table: TLS cert/key paths (spec section 'settings.toml surface')."""

    def test_defaults_to_no_tls(self):
        model = SettingsModel.model_validate({"name": "r", "version": "1.0.0"})
        runtime = model.monitor.to_runtime()
        assert runtime.tls_cert is None
        assert runtime.tls_key is None

    def test_paths_are_expanduser_expanded(self):
        model = SettingsModel.model_validate(
            {
                "name": "r",
                "version": "1.0.0",
                "monitor": {
                    "tls_cert": "~/.otto/tls/monitor-cert.pem",
                    "tls_key": "~/.otto/tls/monitor-key.pem",
                },
            }
        )
        runtime = model.monitor.to_runtime()
        assert runtime.tls_cert == Path.home() / ".otto/tls/monitor-cert.pem"
        assert runtime.tls_key == Path.home() / ".otto/tls/monitor-key.pem"

    def test_cert_without_key_is_allowed(self):
        """A single PEM may bundle cert+key — tls_key stays optional."""
        model = SettingsModel.model_validate(
            {"name": "r", "version": "1.0.0", "monitor": {"tls_cert": "/x/cert.pem"}}
        )
        assert model.monitor.to_runtime().tls_key is None

    def test_key_without_cert_is_rejected(self):
        # Two-halves form (loc line + reason): a bare match="tls_key" was
        # satisfied by pydantic's input_value= echo of the payload itself,
        # whatever rule fired (G4's stated residual, proven by mutation).
        with pytest.raises(
            ValidationError,
            match=r"(?m)^monitor\n\s+Value error, \[monitor\] tls_key is set but tls_cert is not",
        ):
            SettingsModel.model_validate(
                {"name": "r", "version": "1.0.0", "monitor": {"tls_key": "/x/key.pem"}}
            )

    def test_unknown_monitor_key_is_rejected(self):
        """extra='forbid' inherited from OttoModel must cover the new table."""
        with pytest.raises(
            ValidationError,
            match=r"(?m)^monitor\.tls_cret\n\s+Extra inputs are not permitted",
        ):
            SettingsModel.model_validate(
                {"name": "r", "version": "1.0.0", "monitor": {"tls_cret": "/typo.pem"}}
            )


def test_coverage_overrides_block_accepts_file_key():
    from otto.models.settings import CoverageSettingsSpec

    spec = CoverageSettingsSpec.model_validate({"overrides": {"file": "custom/overrides.toml"}})
    assert spec.overrides is not None
    assert spec.overrides.file == "custom/overrides.toml"


def test_coverage_overrides_block_defaults_file_to_none():
    from otto.models.settings import CoverageSettingsSpec

    spec = CoverageSettingsSpec.model_validate({"overrides": {}})
    assert spec.overrides is not None
    assert spec.overrides.file is None


def test_coverage_overrides_block_absent_is_none():
    from otto.models.settings import CoverageSettingsSpec

    spec = CoverageSettingsSpec.model_validate({})
    assert spec.overrides is None


def test_coverage_overrides_unknown_key_fails():
    from otto.models.settings import CoverageSettingsSpec

    with pytest.raises(
        ValidationError, match=r"(?m)^overrides\.path\n\s+Extra inputs are not permitted"
    ):
        CoverageSettingsSpec.model_validate({"overrides": {"path": "x"}})


def test_project_block_accepts_patterns():
    from otto.models.settings import ProjectScopeSpec

    spec = ProjectScopeSpec.model_validate(
        {"lab_patterns": ["tech-.*"], "host_patterns": ["sensor-.*"]}
    )
    assert spec.lab_patterns == ["tech-.*"]


def test_project_block_host_patterns_default_matches_all():
    from otto.models.settings import ProjectScopeSpec

    spec = ProjectScopeSpec.model_validate({"lab_patterns": ["a"]})
    assert spec.host_patterns == [".*"]


def test_project_block_invalid_regex_fails_at_validation():
    from otto.models.settings import ProjectScopeSpec

    with pytest.raises(ValidationError, match="lab_patterns"):
        ProjectScopeSpec.model_validate({"lab_patterns": ["("]})


def test_project_block_invalid_host_regex_fails_at_validation():
    from otto.models.settings import ProjectScopeSpec

    with pytest.raises(ValidationError, match="host_patterns"):
        ProjectScopeSpec.model_validate({"lab_patterns": ["a"], "host_patterns": ["gw-["]})


def test_project_block_regex_error_names_the_pattern():
    """The message must carry the offending pattern; Repo adds the repo name."""
    from otto.models.settings import ProjectScopeSpec

    with pytest.raises(ValidationError, match=r"'gw-\['"):
        ProjectScopeSpec.model_validate({"host_patterns": ["gw-["]})


def test_project_block_absent_is_none():
    from otto.models.settings import SettingsModel

    model = SettingsModel.model_validate({"name": "x", "version": "0.1.0"})
    assert model.project is None


def test_project_block_unknown_key_fails():
    from otto.models.settings import ProjectScopeSpec

    with pytest.raises(ValidationError, match=r"(?m)^labs\n\s+Extra inputs are not permitted"):
        ProjectScopeSpec.model_validate({"labs": ["a"]})


def test_lab_sources_parse_and_reach_repo(tmp_path):
    from otto.config.repo import Repo
    from tests._fixtures.sutrepo import make_sut_repo

    sut = make_sut_repo(
        tmp_path / "sut",
        name="srcrepo",
        extra='[[lab.sources]]\nbackend = "json"\npaths = ["lab_data"]\n',
    )
    repo = Repo(sut)
    (src,) = repo.lab_sources
    assert src.label == "srcrepo/json#1"
    assert src.paths == [sut / "lab_data"]
