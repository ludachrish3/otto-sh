"""power_control is parsed when the host is validated, and built from that parse."""

from types import MappingProxyType
from typing import ClassVar

import pytest
from pydantic import PrivateAttr, ValidationError

from otto.host import (
    Element,
    EmbeddedHostSpec,
    HostValidationError,
    PowerControlSpec,
    UnixHostSpec,
    host_context,
    register_host_class,
    register_power_controller,
)
from otto.host.embedded_host import EmbeddedHost
from otto.host.factory import create_host_from_dict, host_identity, validate_host_dict
from otto.host.power import POWER_CONTROLLERS, CommandPowerController, PowerController
from otto.labs.drops import collecting_drops
from otto.labs.errors import LabRepositoryError
from otto.labs.json_repository import JsonFileLabRepository
from otto.models.base import OttoModel
from otto.registry import BackendRegistry
from tests._fixtures.labdata import write_lab_json

_UNIX = {"ip": "10.0.0.1"}
_TABLE = {"type": "command", "on_cmd": "on {name}", "off_cmd": "off {name}", "controller": "hyp"}
_MALFORMED = {"type": "command", "on_cmd": 5}
_ROUTES = {
    "doctor": lambda d: validate_host_dict(d, element=Element("e")),
    "identity": lambda d: host_identity(d, Element("e")),
    "construction": lambda d: create_host_from_dict(d, element=Element("e")),
}


class _Recorder(PowerController):
    built: ClassVar[list[object]] = []

    async def on(self, host):  # pragma: no cover - never called
        raise NotImplementedError

    async def off(self, host):  # pragma: no cover - never called
        raise NotImplementedError


class _SecretConfig(OttoModel):
    password: int


class _Uncopyable(OttoModel):
    _gen: object = PrivateAttr(default_factory=lambda: (i for i in ()))


def _recording_factory(c):
    _Recorder.built.append(c.config)
    return _Recorder()


def test_a_table_validates_to_a_power_control_spec_and_dumps_as_written():
    spec = UnixHostSpec.model_validate({**_UNIX, "power_control": _TABLE})
    assert isinstance(spec.power_control, PowerControlSpec)
    assert spec.model_dump()["power_control"] == _TABLE
    named = UnixHostSpec.model_validate({**_UNIX, "power_control": "command"})
    assert named.model_dump()["power_control"] == "command"


def test_a_table_given_as_any_mapping_validates_like_a_dict():
    """A library caller's read-only mapping is a table, not a name."""
    spec = UnixHostSpec.model_validate({**_UNIX, "power_control": MappingProxyType(_TABLE)})
    assert isinstance(spec.power_control, PowerControlSpec)
    assert spec.model_dump()["power_control"] == _TABLE


def test_the_json_schema_shows_all_three_forms():
    """A table or a name (one of the two, by the value's shape), or null."""
    table_or_name, null = UnixHostSpec.model_json_schema()["properties"]["power_control"]["anyOf"]
    assert null == {"type": "null"}
    assert table_or_name == {"oneOf": [{"$ref": "#/$defs/PowerControlSpec"}, {"type": "string"}]}


def test_the_built_host_carries_the_controller_built_from_the_parse():
    host = create_host_from_dict({**_UNIX, "power_control": _TABLE}, element=Element("e"))
    assert isinstance(host.power_control, CommandPowerController)
    assert host.power_control.config.controller == "hyp"


def _build_kwargs(entry):
    hc = host_context(entry, element=Element("e"))
    return hc.spec.build_kwargs(hc.spec.validate_in(hc.context))


def test_an_unset_power_control_keeps_the_class_default_and_none_forwards_none():
    """The class default is None too, so the two are told apart at the constructor's arguments."""
    assert "power_control" not in _build_kwargs(dict(_UNIX))
    assert _build_kwargs({**_UNIX, "power_control": None})["power_control"] is None


@pytest.mark.parametrize("route", _ROUTES.values(), ids=_ROUTES.keys())
def test_a_malformed_table_is_reported_on_every_route_naming_the_host(route):
    with pytest.raises(
        HostValidationError, match=r"host 'e': .*power controller 'command'.*parse failed"
    ):
        route({**_UNIX, "power_control": _MALFORMED})


def test_a_parse_error_never_quotes_the_rejected_value():
    register_power_controller("t-secret", config=_SecretConfig, factory=_recording_factory)
    with pytest.raises(
        HostValidationError, match=r"host 'e': .*power controller 't-secret'.*parse failed"
    ) as exc:
        validate_host_dict(
            {**_UNIX, "power_control": {"type": "t-secret", "password": "s3cret-value"}},
            element=Element("e"),
        )
    assert "s3cret-value" not in str(exc.value)


def test_no_controller_is_built_during_validation():
    _Recorder.built.clear()
    register_power_controller("t-rec", config=_SecretConfig, factory=_recording_factory)
    entry = {**_UNIX, "power_control": {"type": "t-rec", "password": 1}}
    validate_host_dict(dict(entry), element=Element("e"))
    host_identity(dict(entry), Element("e"))
    assert _Recorder.built == []
    create_host_from_dict(dict(entry), element=Element("e"))
    assert len(_Recorder.built) == 1


@pytest.mark.parametrize("route", _ROUTES.values(), ids=_ROUTES.keys())
def test_an_uncopyable_config_fails_validation_naming_the_host(route):
    _Recorder.built.clear()
    register_power_controller("t-uncopyable", config=_Uncopyable, factory=_recording_factory)
    with pytest.raises(
        HostValidationError, match=r"host 'e': its validated configuration cannot be copied"
    ) as exc:
        route({**_UNIX, "power_control": "t-uncopyable"})
    assert isinstance(exc.value.__cause__, TypeError)
    assert _Recorder.built == []  # refused before construction, on every route


def test_the_build_s_prepared_power_passes_the_registry_s_provenance_check():
    hc = host_context({**_UNIX, "power_control": _TABLE}, element=Element("e"))
    ready = hc.spec.validate_in(hc.context)
    power = ready._payload.power
    assert power.registry is POWER_CONTROLLERS
    assert isinstance(POWER_CONTROLLERS.build(power), CommandPowerController)


def test_stock_checks_then_check_in_once_then_preparation(monkeypatch):
    events: list[str] = []
    real_prepare = BackendRegistry.prepare

    def spy(self, name, raw, env, *, source=None):
        if self is POWER_CONTROLLERS:
            events.append("prepare")
        return real_prepare(self, name, raw, env, source=source)

    monkeypatch.setattr(BackendRegistry, "prepare", spy)

    class Order(EmbeddedHostSpec):
        def check_in(self, ctx):
            events.append("check_in")
            if self.name == "refuse":
                raise ValueError("refused by check_in")

    register_host_class("t-order", EmbeddedHost, spec=Order)
    base = {"ip": "192.0.2.1", "os_type": "t-order", "power_control": _TABLE}
    with pytest.raises(HostValidationError, match="supplies no command_frame"):
        validate_host_dict(dict(base), element=Element("e"))  # a stock contextual failure
    assert events == []
    with pytest.raises(HostValidationError, match="refused by check_in"):
        validate_host_dict(
            {**base, "command_frame": "zephyr", "name": "refuse"}, element=Element("e")
        )
    assert events == ["check_in"]
    events.clear()
    validate_host_dict({**base, "command_frame": "zephyr"}, element=Element("e"))
    assert events == ["check_in", "prepare"]


def _lab(tmp_path, power_control):
    hosts = [
        {
            **_UNIX,
            "creds": [{"login": "u", "password": "p"}],
            "power_control": power_control,
            "element": "p",
            "labs": ["l"],
        }
    ]
    write_lab_json(tmp_path / "lab.json", hosts, links=[])
    return JsonFileLabRepository(search_paths=[tmp_path])


@pytest.mark.parametrize("kind", ["malformed", "uncopyable"])
def test_the_loader_reports_a_bad_power_table_as_a_validation_error(tmp_path, kind):
    register_power_controller("t-uncopyable", config=_Uncopyable, factory=_recording_factory)
    project = _lab(tmp_path, _MALFORMED if kind == "malformed" else "t-uncopyable")
    with pytest.raises(
        LabRepositoryError, match=r"element 'p' hosts\[0\] in lab 'l': host 'p'"
    ) as exc:
        project.load_lab("l")
    assert isinstance(exc.value.__cause__, HostValidationError)


@pytest.mark.parametrize("kind", ["malformed", "uncopyable"])
def test_summaries_drop_a_host_whose_power_table_fails(tmp_path, kind):
    register_power_controller("t-uncopyable", config=_Uncopyable, factory=_recording_factory)
    project = _lab(tmp_path, _MALFORMED if kind == "malformed" else "t-uncopyable")
    with collecting_drops() as drops:
        assert project.list_host_summaries() == []
    reason = "parse failed" if kind == "malformed" else "cannot be copied"
    assert any("element 'p' hosts[0]" in d.where and reason in d.reason for d in drops)


def test_a_summary_keeps_a_host_whose_controller_is_registered_and_records_the_drop_otherwise(
    tmp_path,
):
    """Review Focus 5."""
    register_power_controller("t-project", config=_SecretConfig, factory=_recording_factory)
    project = _lab(tmp_path, {"type": "t-project", "password": 1})
    assert {s.id for s in project.list_host_summaries()} == {"p"}
    POWER_CONTROLLERS.unregister("t-project")
    with collecting_drops() as drops:
        assert project.list_host_summaries() == []
    assert any("t-project" in d.reason for d in drops)


_COPY_SECRET = "COPY-SECRET-SENTINEL"


class _CopyLeaksASecret(OttoModel):
    """A parsed config whose copy hook fails with a ValidationError quoting a secret."""

    def __deepcopy__(self, memo):
        _SecretConfig.model_validate({"password": _COPY_SECRET})


def _validate_in_context(d):
    hc = host_context(d, element=Element("e"))
    return hc.spec.validate_in(hc.context)


def test_a_copy_failure_never_quotes_its_cause_on_any_route(tmp_path):
    """The cause stays chained; its message, which may quote a rejected value, stays out."""
    register_power_controller("t-copy-leak", config=_CopyLeaksASecret, factory=_recording_factory)
    entry = {**_UNIX, "power_control": "t-copy-leak"}
    messages: dict[str, str] = {}
    for name, route in {**_ROUTES, "host_context": _validate_in_context}.items():
        with pytest.raises(HostValidationError, match=r"^host 'e': .*cannot be copied") as exc:
            route(dict(entry))
        assert isinstance(exc.value.__cause__, ValidationError), name
        messages[name] = str(exc.value)
    project = _lab(tmp_path, "t-copy-leak")
    with pytest.raises(LabRepositoryError) as loaded:
        project.load_lab("l")
    messages["loader"] = str(loaded.value)
    with collecting_drops() as drops:
        assert project.list_host_summaries() == []
    messages["summaries"] = " | ".join(d.reason for d in drops)
    for name, message in messages.items():
        assert "cannot be copied for construction: ValidationError" in message, name
        assert _COPY_SECRET not in message, name


_TABLE_SECRET = "TABLE-SECRET-SENTINEL"


@pytest.mark.parametrize(
    "table",
    [
        {"password": _TABLE_SECRET},
        {"type": 5, "password": _TABLE_SECRET},
        {"type": "nope", "password": _TABLE_SECRET},
        MappingProxyType({"password": _TABLE_SECRET}),
    ],
    ids=["missing-type", "invalid-type", "unregistered-type", "missing-type-mapping"],
)
def test_a_table_with_a_bad_type_never_quotes_the_table_on_any_route(tmp_path, table):
    """One error, on the table's type; the rest of the table stays out of every message."""
    entry = {**_UNIX, "power_control": table}
    messages: dict[str, str] = {}
    for name, route in {**_ROUTES, "host_context": _validate_in_context}.items():
        with pytest.raises(ValidationError, match=r"power_control\.table\.type") as exc:
            route(dict(entry))
        assert [e["loc"] for e in exc.value.errors()] == [("power_control", "table", "type")], name
        messages[name] = str(exc.value)
    project = _lab(tmp_path, dict(table))  # a lab file holds JSON objects
    with pytest.raises(LabRepositoryError) as loaded:
        project.load_lab("l")
    messages["loader"] = str(loaded.value)
    with collecting_drops() as drops:
        assert project.list_host_summaries() == []
    messages["summaries"] = " | ".join(d.reason for d in drops)
    for name, message in messages.items():
        assert "power_control.table.type" in message, name
        assert _TABLE_SECRET not in message, name
    for name in ["loader", "summaries"]:  # otto's own renderings quote no input at all
        assert "input_value" not in messages[name], name
