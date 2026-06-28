from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from otto.configmodule.repo import DockerCompose, DockerImage, DockerSettings
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
                    "default_host": "pepper_seed",
                    "services": ["api", "worker"],
                }
            ],
        }
    )
    rt = spec.to_runtime()
    assert isinstance(rt.composes[0], DockerCompose)
    assert rt.composes[0].path == Path("/repo/compose.yml")
    assert rt.composes[0].default_host == "pepper_seed"
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
    with pytest.raises(ValidationError):
        DockerSettingsSpec.model_validate({"registy_url": "x"})  # typo


def test_docker_image_spec_requires_name_dockerfile_context():
    with pytest.raises(ValidationError):
        DockerImageSpec.model_validate({"name": "api"})  # missing dockerfile/context


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
    with pytest.raises(ValidationError):
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
    with pytest.raises(ValidationError):
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
    with pytest.raises(ValidationError):
        ReservationFile.model_validate({"version": 2, "reservations": []})


def test_reservation_file_rejects_malformed_expires():
    # a bad timestamp surfaces as ValidationError (the validator must not swallow
    # the underlying ValueError from datetime.fromisoformat).
    with pytest.raises(ValidationError):
        ReservationFile.model_validate(
            {
                "version": 1,
                "reservations": [{"user": "a", "resources": ["r"], "expires": "not-a-date"}],
            }
        )


def test_reservation_file_rejects_non_string_resources():
    with pytest.raises(ValidationError):
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
    with pytest.raises(ValidationError) as exc:
        SettingsModel.model_validate({"name": "repo1"})  # no version
    assert "version" in str(exc.value)


def test_settings_rejects_bad_version_format():
    with pytest.raises(ValidationError):
        SettingsModel.model_validate({"name": "r", "version": "1.0"})  # not X.Y.Z


def test_settings_version_allows_semver_suffix():
    # deliberate prefix match (consistent with the runtime Version parser): a
    # trailing SemVer suffix is accepted, not rejected.
    m = SettingsModel.model_validate({"name": "r", "version": "1.2.3-rc1"})
    assert m.version == "1.2.3-rc1"


def test_settings_allows_legacy_lab_data_type_and_opaque_coverage():
    m = SettingsModel.model_validate(
        {
            **_minimal(),
            "lab_data_type": "json",
            "coverage": {"gcda_remote_dir": "/var/cov", "embedded": {"extension": "cov"}},
        }
    )
    assert m.lab_data_type == "json"
    assert m.coverage == {"gcda_remote_dir": "/var/cov", "embedded": {"extension": "cov"}}


def test_settings_forbids_unknown_top_level_key():
    with pytest.raises(ValidationError) as exc:
        SettingsModel.model_validate({**_minimal(), "labz": []})  # typo: labs
    assert "labz" in str(exc.value)


def test_settings_paths_coerce_to_path_lists():
    m = SettingsModel.model_validate(
        {
            **_minimal(),
            "labs": ["/a/lab"],
            "libs": ["/a/lib"],
            "tests": ["/a/tests"],
            "init": ["mod_a"],
            "valid_labs": ["embedded"],
        }
    )
    assert m.labs == [Path("/a/lab")]
    assert m.init == ["mod_a"]
    assert m.valid_labs == ["embedded"]


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
    with pytest.raises(ValueError, match="unknown .host_preferences. key 'bogus'"):
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
    with pytest.raises(ValueError):
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
        # ${sut_dir} left as-is; it doesn't affect top-level key validation
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
    from otto.models.settings import _HOST_DEFAULT_OPTION_SPECS
    from otto.storage.factory import OPTIONS_KEYS

    assert set(_HOST_DEFAULT_OPTION_SPECS) == OPTIONS_KEYS


def test_docker_spec_fields_match_runtime_dataclass():
    """Bidirectional drift guard: each docker spec's field names match its
    runtime dataclass's init fields (so a field added to one but not the other
    is caught), mirroring HOST_SPEC_RUNTIME_PAIRS in test_host_specs.py.
    """
    import dataclasses

    from otto.configmodule.repo import DockerCompose, DockerImage, DockerSettings
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


# ---------------------------------------------------------------------------
# Task 4 (Plan A): LabConfigSpec + SettingsModel.lab wiring
# ---------------------------------------------------------------------------


def test_lab_config_spec_defaults_to_json():
    cfg = LabConfigSpec.model_validate({})
    assert cfg.backend == "json"
    assert cfg.model_extra == {}


def test_lab_config_spec_keeps_backend_subtable_open():
    cfg = LabConfigSpec.model_validate({"backend": "myteam", "myteam": {"url": "https://cmdb"}})
    assert cfg.backend == "myteam"
    assert cfg.model_extra == {"myteam": {"url": "https://cmdb"}}


def test_settings_model_accepts_lab_block():
    m = SettingsModel.model_validate(
        {"name": "demo", "version": "1.0.0", "lab": {"backend": "json"}}
    )
    assert m.lab.backend == "json"


def test_settings_model_lab_defaults_when_absent():
    m = SettingsModel.model_validate({"name": "demo", "version": "1.0.0"})
    assert m.lab.backend == "json"
