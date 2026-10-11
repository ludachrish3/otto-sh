"""Construction consumes one record that contextual validation issued for this spec."""

import dataclasses

import pytest

from otto.host import Element, HostValidationError, UnixHostSpec, host_context, register_host_class
from otto.host.construction import _open_record
from otto.host.os_profile import OsProfile, ProfileContext, ProfileFields
from otto.host.unix_host import UnixHost
from otto.registry import FrozenMap
from tests._fixtures.host_build import build_host

_UNIX = {"ip": "10.0.0.1", "creds": [{"login": "u", "password": "p"}]}


def _ready(entry=_UNIX, **kw):
    hc = host_context(dict(entry), element=Element("lab"), **kw)
    return hc, hc.spec.validate_in(hc.context)


def test_host_context_returns_the_spec_and_its_context():
    hc = host_context(dict(_UNIX), element=Element("lab"))
    assert isinstance(hc.spec, UnixHostSpec)
    assert hc.context.host_id == "lab"
    with pytest.raises(dataclasses.FrozenInstanceError):
        hc.spec = hc.spec  # ty: ignore[invalid-assignment]


def test_a_stock_build_consumes_its_own_record_once():
    hc, ready = _ready()
    assert hc.spec.to_host(ready=ready).ip == "10.0.0.1"
    with pytest.raises(
        HostValidationError, match=r"host 'lab': record already consumed; validate again"
    ):
        hc.spec.to_host(ready=ready)


def test_a_record_from_another_spec_instance_is_refused():
    hc, _ = _ready()
    _, other = _ready()
    with pytest.raises(HostValidationError, match=r"issued for another spec"):
        hc.spec.to_host(ready=other)


def test_a_record_from_another_spec_class_is_refused():
    hc, _ = _ready()
    _, embedded = _ready({"ip": "192.0.2.1", "os_type": "zephyr"})
    with pytest.raises(HostValidationError, match=r"issued for another spec"):
        hc.spec.to_host(ready=embedded)


def test_a_probe_record_cannot_build():
    """No-preference mode (what validate_host_dict and host_identity run) issues a probe record."""
    hc = host_context(dict(_UNIX), element=Element("lab"))
    probe = hc.spec.validate_in(dataclasses.replace(hc.context, preferences=None))
    assert probe.selections is None
    with pytest.raises(
        HostValidationError,
        match=r"no-preference validation .* cannot build \(it validated through "
        r"\{'term': 'ssh', 'transfer': 'scp', 'impairer': 'netem'\}\)",
    ):
        hc.spec.to_host(ready=probe)


@dataclasses.dataclass(slots=True, kw_only=True)
class _FailingHost(UnixHost):
    def __post_init__(self) -> None:
        raise RuntimeError("constructor failed")


def test_a_record_stays_consumed_after_a_failed_build():
    register_host_class("t-failing", _FailingHost)
    hc, ready = _ready({**_UNIX, "os_type": "t-failing"})
    with pytest.raises(RuntimeError, match="constructor failed"):
        hc.spec.to_host(ready=ready)
    with pytest.raises(HostValidationError, match="record already consumed"):
        hc.spec.to_host(ready=ready)


def test_mutating_the_spec_after_validation_does_not_change_the_host():
    hc, ready = _ready()
    hc.spec.ip = "10.9.9.9"  # OttoModel is not frozen
    hc.spec.creds.clear()
    host = hc.spec.to_host(ready=ready)
    assert host.ip == "10.0.0.1"
    assert [c.login for c in host.creds] == ["u"]


def test_mutating_the_context_after_validation_does_not_reach_the_build():
    """What a Convert or Resolve callback reads through build.context is the validated copy."""
    hc, ready = _ready(preferences={".*": {"term": ["telnet"]}})
    hc.context.preferences["term"] = ["ssh"]  # a dict inside the frozen context
    build = _open_record(ready, hc.spec)
    assert build.context.preferences == {"term": ["telnet"]}
    assert build.context is not hc.context
    assert build.context.element is hc.context.element


def test_mutating_the_callers_preferences_after_validation_does_not_change_the_build():
    """The caller keeps its preferences; editing them in place after validate_in changes nothing.

    The selection list is copied by select_preferences into the context, and the context again
    into the record. The option table is copied four times before it reaches the record:
    select_option_defaults builds new tables, _merge_host_dict spreads them, the structural parse
    turns the table into a TelnetOptionsSpec, and the record deep-copies the values. Skipping the
    structural parse alone reddens the build.inputs assertion, with an AttributeError, because the
    table then reaches the record as a plain dict; dropping any one of the other three copies
    leaves the alias broken by the rest. The assertion pins all four together.
    """
    prefs = {".*": {"term": ["telnet"], "telnet_options": {"port": 2323}}}
    hc = host_context(dict(_UNIX), element=Element("lab"), preferences=prefs)
    ready = hc.spec.validate_in(hc.context)
    prefs[".*"]["term"][0] = "ssh"  # the nested selection list, in place
    prefs[".*"]["term"].append("telnet")
    prefs[".*"]["telnet_options"]["port"] = 1  # the option table, in place
    build = _open_record(ready, hc.spec)
    # The record's copy first: a context kept by identity fails here, not at the next line.
    assert build.context.preferences == {"term": ["telnet"]}
    assert hc.context.preferences == {"term": ["telnet"]}
    assert build.inputs["telnet_options"].port == 2323
    assert build.selections["term"] == "telnet"


def test_selections_are_read_only():
    _, ready = _ready(preferences={})
    with pytest.raises(TypeError):
        ready.selections["term"] = "telnet"  # ty: ignore[invalid-assignment]


def test_a_context_resolved_for_a_base_spec_is_refused():
    """A subclass whose own fields coverage never checked cannot reach the class."""

    class Sub(UnixHostSpec):
        pass

    hc = host_context(dict(_UNIX), element=Element("lab"))
    with pytest.raises(HostValidationError, match=r"resolved for UnixHostSpec, not Sub"):
        Sub.model_validate(dict(_UNIX)).validate_in(hc.context)


def test_a_context_whose_host_id_disagrees_is_refused():
    hc = host_context({**_UNIX, "board": "cpu", "slot": 1}, element=Element("lab"))
    with pytest.raises(HostValidationError, match=r"the context names host 'lab'"):
        hc.spec.validate_in(dataclasses.replace(hc.context, host_id="lab"))


def test_a_direct_build_keeps_its_data_profile_prompts():
    """host_context has no factory scope around it; the stock builder scopes itself."""
    alpha = OsProfile("alpha", "unix", ProfileFields(login_prompt="alpha-login: "))
    data = ProfileContext(profiles=FrozenMap({"alpha": alpha}), owners=FrozenMap({"alpha": "r"}))
    host = build_host({**_UNIX, "os_type": "alpha"}, profiles=data)
    assert host._profile_fields is not None
    assert host._profile_fields.login_prompt == "alpha-login: "
