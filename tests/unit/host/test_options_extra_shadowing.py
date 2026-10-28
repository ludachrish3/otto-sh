"""An ``extra`` key that names a curated field is refused, so the field is the one source.

``extra`` is the pass-through for library kwargs otto has not curated. Letting
it also carry a curated field's name meant two sources for one value: the
survey read ``ssh_options.port`` while the connect dialled ``extra["port"]``,
and every reader of a field had to remember to look in ``extra`` too. Now the
options dataclass refuses the collision when it is built, and the lab.json
spec refuses it before the host is built, both through one helper.
"""

import dataclasses

import pytest
from pydantic import ValidationError

from otto.host import option_specs, options
from otto.host.options import (
    ConsoleOptions,
    FtpOptions,
    ScpOptions,
    SftpOptions,
    SshOptions,
    TelnetOptions,
    shadowed_fields,
)

RUNTIME = [SshOptions, TelnetOptions, ConsoleOptions, SftpOptions, ScpOptions, FtpOptions]


@pytest.mark.parametrize("cls", RUNTIME, ids=lambda c: c.__name__)
def test_every_options_class_with_an_extra_refuses_a_field_name_in_it(cls):
    """Pick a real curated field of each class; its name in ``extra`` is refused by name."""
    curated = next(
        f.name for f in dataclasses.fields(cls) if f.name not in ("extra", "post_connect")
    )
    with pytest.raises(ValueError, match=rf"{cls.__name__}.*extra.*\b{curated}\b"):
        cls(extra={curated: object()})


def test_the_port_cannot_be_set_twice():
    with pytest.raises(ValueError, match="port"):
        SshOptions(port=22, extra={"port": 2222})


def test_a_key_that_names_no_field_still_passes_through():
    opts = SshOptions(extra={"config": ["/tmp/ssh_config"], "proxy_command": "pc"})
    kw = opts._kwargs()
    assert kw["config"] == ["/tmp/ssh_config"]
    assert kw["proxy_command"] == "pc"


def test_shadowed_fields_is_the_one_rule_both_boundaries_use():
    assert shadowed_fields({"port": 1, "config": []}, SshOptions) == ["port"]
    assert shadowed_fields({"config": []}, SshOptions) == []
    # ``extra`` naming itself is as shadowed as any field.
    assert shadowed_fields({"extra": {}}, SshOptions) == ["extra"]


@pytest.mark.parametrize(
    ("spec", "field_name"),
    [
        (option_specs.SshOptionsSpec, "port"),
        (option_specs.TelnetOptionsSpec, "port"),
        (option_specs.SftpOptionsSpec, "env"),
        (option_specs.ScpOptionsSpec, "preserve"),
        (option_specs.FtpOptionsSpec, "port"),
    ],
)
def test_the_lab_spec_refuses_the_same_collision_before_the_host_is_built(spec, field_name):
    with pytest.raises(ValidationError, match=field_name):
        spec.model_validate({"extra": {field_name: 1}})


def test_the_identity_keys_still_warn_rather_than_refuse(caplog):
    """``username``/``password``/``tunnel`` are the call sites' kwargs, not fields: the
    warning in ``_kwargs`` stays as it was."""
    import logging

    opts = SshOptions(extra={"username": "x"})
    with caplog.at_level(logging.WARNING, logger="otto.host.options"):
        opts._kwargs()
    assert "username" in caplog.text


def test_the_module_exposes_the_rule_for_the_spec_layer():
    assert options.shadowed_fields is shadowed_fields


@pytest.mark.parametrize("key", ["password", "passphrase"])
def test_ssh_extra_naming_a_secret_is_refused_at_runtime_without_echoing_it(key):
    """A password must reach asyncssh only from the host's creds, still wrapped.

    ``extra`` is a plain dict spread into the connect kwargs, so a password in
    it would sit raw in every otto frame between the options and the library.
    The key is refused through the same rule as a curated field's name, and
    the refusal names the key, never the value.
    """
    with pytest.raises(ValueError, match=rf"SshOptions.*extra.*\b{key}\b") as ei:
        SshOptions(extra={key: "PLANTED-SECRET-7f3a"})
    assert "PLANTED-SECRET-7f3a" not in str(ei.value)


@pytest.mark.parametrize("key", ["password", "passphrase"])
def test_ssh_extra_naming_a_secret_is_refused_by_the_lab_spec(key):
    with pytest.raises(ValidationError, match=key):
        option_specs.SshOptionsSpec.model_validate({"extra": {key: "PLANTED-SECRET-7f3a"}})


def test_reserved_extra_names_are_part_of_the_one_rule():
    assert shadowed_fields({"password": 1, "passphrase": 2, "config": []}, SshOptions) == [
        "passphrase",
        "password",
    ]


@pytest.mark.parametrize("cls", RUNTIME, ids=lambda c: c.__name__)
def test_every_options_class_declares_its_reserved_extra_names(cls):
    """One declaration per class, read by ``shadowed_fields``: empty unless reserved."""
    expected = {"password", "passphrase"} if cls is SshOptions else set()
    assert set(cls._reserved_extra) == expected


@pytest.mark.parametrize("key", ["password", "passphrase"])
def test_the_lab_boundary_rendering_of_the_refusal_never_shows_the_value(key):
    """What a lab reader prints is ``compact_validation_error``'s rendering, not the raw error."""
    from otto.models.base import compact_validation_error

    with pytest.raises(ValidationError, match=key) as ei:
        option_specs.SshOptionsSpec.model_validate({"extra": {key: "PLANTED-SECRET-7f3a"}})
    shown = compact_validation_error(ei.value)
    assert key in shown
    assert "PLANTED-SECRET-7f3a" not in shown


def test_the_passphrase_field_is_the_route_extra_cannot_bypass():
    spec = option_specs.SshOptionsSpec.model_validate({"passphrase": "PLANTED-SECRET-7f3a"})
    runtime = spec.to_runtime()
    assert runtime.passphrase is not None
    assert runtime.passphrase.get_secret_value() == "PLANTED-SECRET-7f3a"
    assert "PLANTED-SECRET-7f3a" not in repr(runtime)
    with pytest.raises(ValueError, match="passphrase"):
        SshOptions(passphrase="x", extra={"passphrase": "y"})
