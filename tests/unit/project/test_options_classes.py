"""The six first-party options classes: the flags each default instruction has today."""

import dataclasses

import pytest

from otto.params import options_params
from otto.project import (
    CleanupOptions,
    GetLogsOptions,
    InstallOptions,
    InstallState,
    InstallToolsOptions,
    StatusOptions,
    UninstallOptions,
    combine_install_states,
)


@pytest.mark.parametrize(
    ("cls", "defaults"),
    [
        (InstallOptions, {"ensure": False, "recover_partial": True}),
        (UninstallOptions, {"product_logs": True, "debug_logs": True}),
        (
            CleanupOptions,
            {
                "product_logs": True,
                "debug_logs": True,
                "reset_impairments": True,
                "remove_tunnels": True,
            },
        ),
        (GetLogsOptions, {"product_logs": True, "debug_logs": True, "require_product_logs": False}),
        (InstallToolsOptions, {"dev": True, "toolchain": False}),
        (StatusOptions, {"full": False}),
    ],
)
def test_fields_and_defaults(cls, defaults) -> None:
    assert {f.name: f.default for f in dataclasses.fields(cls)} == defaults
    assert dataclasses.asdict(cls()) == defaults


def test_every_field_expands_to_a_typer_option() -> None:
    for cls in (
        InstallOptions,
        UninstallOptions,
        CleanupOptions,
        GetLogsOptions,
        InstallToolsOptions,
        StatusOptions,
    ):
        assert [p.name for p in options_params(cls)] == [f.name for f in dataclasses.fields(cls)]


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ({}, InstallState.UNINSTALLED),
        ({"a": InstallState.INSTALLED}, InstallState.INSTALLED),
        ({"a": InstallState.UNINSTALLED, "b": InstallState.UNINSTALLED}, InstallState.UNINSTALLED),
        ({"a": InstallState.INSTALLED, "b": InstallState.UNINSTALLED}, InstallState.PARTIAL),
        ({"a": InstallState.PARTIAL}, InstallState.PARTIAL),
    ],
)
def test_combine_install_states(states, expected) -> None:
    assert combine_install_states(states) is expected
