"""Checks that need the selection, the class or the id run in context, in two modes."""

import dataclasses
from typing import ClassVar

import pytest
from pydantic import Field, TypeAdapter

from otto.host import (
    Element,
    EmbeddedHostSpec,
    HostValidationError,
    UnixHostSpec,
    register_host_class,
)
from otto.host.embedded_host import EmbeddedHost
from otto.host.factory import create_host_from_dict, host_identity, validate_host_dict
from otto.host.os_profile import OsProfile, ProfileContext, ProfileFields
from otto.host.unix_host import UnixHost
from otto.labs.drops import collecting_drops
from otto.labs.errors import LabRepositoryError
from otto.labs.json_repository import JsonFileLabRepository
from otto.link import LinkImpairer, register_impairer
from otto.registry import FrozenMap
from tests._fixtures.host_build import build_host
from tests._fixtures.labdata import write_lab_json

_SSH_CREDS = [{"login": "u", "password": "p"}]
_CONSOLE_ONLY_CREDS = [{"login": "c", "password": "p", "protocols": ["console"]}]
_EITHER = {"ip": "10.0.0.9", "name": "cons1", "valid_terms": ["console", "ssh"]}
_FRAMELESS = {"ip": "192.0.2.9", "os_type": "embedded"}
_ROUTES = {
    "doctor": lambda d: validate_host_dict(d, element=Element("e")),
    "identity": lambda d: host_identity(d, Element("e")),
    "construction": lambda d: create_host_from_dict(d, element=Element("e")),
}


def test_a_console_menu_with_no_server_passes_the_doctor_through_its_ssh_witness():
    validate_host_dict({**_EITHER, "creds": _SSH_CREDS}, element=Element("e"))


def test_the_same_entry_loads_without_preferences_only_if_the_resolver_s_pick_passes():
    with pytest.raises(
        HostValidationError, match=r"term 'console' requires console_options\.server"
    ):
        create_host_from_dict({**_EITHER, "creds": _SSH_CREDS}, element=Element("e"))


def test_the_same_entry_loads_under_an_ssh_preference_and_fails_under_console():
    assert build_host({**_EITHER, "creds": _SSH_CREDS}, selections={"term": ["ssh"]}).term == "ssh"
    with pytest.raises(HostValidationError, match=r"console_options\.server"):
        build_host({**_EITHER, "creds": _SSH_CREDS}, selections={"term": ["console"]})


def test_no_single_term_passes_both_checks_so_the_doctor_refuses():
    """console fails the console rule; ssh fails the cred term-candidate check."""
    with pytest.raises(
        HostValidationError, match=r"host 'e': term 'console' requires console_options\.server"
    ):
        validate_host_dict({**_EITHER, "creds": _CONSOLE_ONLY_CREDS}, element=Element("e"))


@pytest.mark.parametrize("route", _ROUTES.values(), ids=_ROUTES.keys())
def test_a_witness_must_be_a_selection_the_resolver_can_produce(route):
    """A console-only host pinned to nc: every console build refuses the pin.

    So scp, which passes the selection checks on its own, is no witness.
    """
    entry = {
        "ip": "10.0.0.9",
        "name": "cons1",
        "creds": _SSH_CREDS,
        "valid_terms": ["console"],
        "console_options": {"server": "test1", "port": 4001},
        "valid_transfers": ["nc", "scp"],
        "transfer": "nc",
    }
    with pytest.raises(HostValidationError, match=r"cons1.*second command channel"):
        route(entry)


# Two entries only context refuses: a frameless embedded host, and the console+ssh menu
# with console-only creds (no single term passes both selection checks).
_REFUSED = {
    "frameless": (_FRAMELESS, "supplies no command_frame"),
    "combination": (
        {**_EITHER, "creds": _CONSOLE_ONLY_CREDS},
        "term 'console' requires console_options.server",
    ),
}


@pytest.mark.parametrize(("refused", "reason"), _REFUSED.values(), ids=_REFUSED.keys())
def test_a_refused_entry_drops_out_of_summaries_with_a_recorded_drop(tmp_path, refused, reason):
    hosts = [
        {**refused, "element": "bad", "labs": ["l"]},
        {"ip": "10.0.0.2", "creds": _SSH_CREDS, "element": "good", "labs": ["l"]},
    ]
    write_lab_json(tmp_path / "lab.json", hosts, links=[])
    with collecting_drops() as drops:
        ids = {s.id for s in JsonFileLabRepository(search_paths=[tmp_path]).list_host_summaries()}
    assert ids == {"good"}
    assert any("element 'bad' hosts[0]" in d.where and reason in d.reason for d in drops)


@pytest.mark.parametrize("refused", [r for r, _ in _REFUSED.values()], ids=_REFUSED.keys())
def test_a_refused_entry_is_not_addressable_by_a_declared_link(tmp_path, refused):
    """The addressing walk runs host_identity per record; a refused record has no address."""
    hosts = [
        {"ip": "10.0.0.2", "creds": _SSH_CREDS, "element": "good", "labs": ["l"]},
        {**refused, "element": "bad", "labs": ["m"]},
    ]
    links = [{"endpoints": [{"host": "good"}, {"host": "bad"}], "protocol": "udp"}]
    write_lab_json(tmp_path / "lab.json", hosts, links=links)
    with pytest.raises(LabRepositoryError, match=r"unknown host 'bad'"):
        JsonFileLabRepository(search_paths=[tmp_path]).load_lab("l")


def test_an_entry_that_loads_under_its_real_preferences_stays_in_summaries(tmp_path):
    hosts = [{**_EITHER, "creds": _SSH_CREDS, "element": "either", "labs": ["l"]}]
    write_lab_json(tmp_path / "lab.json", hosts, links=[])
    summaries = JsonFileLabRepository(search_paths=[tmp_path]).list_host_summaries()
    assert {s.id for s in summaries} == {"either"}


def test_model_validate_alone_now_accepts_what_only_context_refuses():
    UnixHostSpec.model_validate({**_EITHER, "creds": _CONSOLE_ONLY_CREDS})


def test_an_omitted_frame_passes_for_zephyr_and_fails_for_embedded():
    validate_host_dict({"ip": "192.0.2.1", "os_type": "zephyr"}, element=Element("z"))
    with pytest.raises(
        HostValidationError,
        match=r"host 'z': os_type 'embedded' builds a EmbeddedHost, "
        r"which supplies no command_frame",
    ):
        validate_host_dict({"ip": "192.0.2.1", "os_type": "embedded"}, element=Element("z"))


def test_a_data_profile_frame_satisfies_the_embedded_frame_rule():
    """Review Focus 2."""
    framed = OsProfile(
        "rtos", "embedded", ProfileFields(FrozenMap.freeze_json({"command_frame": "zephyr"}))
    )
    bare = OsProfile("rtos-bare", "embedded", ProfileFields())
    data = ProfileContext(
        profiles=FrozenMap({"rtos": framed, "rtos-bare": bare}),
        owners=FrozenMap({"rtos": "r", "rtos-bare": "r"}),
    )
    validate_host_dict({"ip": "192.0.2.1", "os_type": "rtos"}, element=Element("z"), profiles=data)
    with pytest.raises(HostValidationError, match="supplies no command_frame"):
        validate_host_dict(
            {"ip": "192.0.2.1", "os_type": "rtos-bare"}, element=Element("z"), profiles=data
        )


class _DefaultFrameSpec(EmbeddedHostSpec):
    """A spec whose own default names a frame; an unset field is never forwarded."""

    command_frame: str | None = "zephyr"


@pytest.mark.parametrize("route", _ROUTES.values(), ids=_ROUTES.keys())
def test_a_spec_default_frame_does_not_satisfy_the_frame_rule(route):
    """Only a frame the entry sets reaches the host; else the class must supply one."""
    register_host_class("t-default-frame", EmbeddedHost, spec=_DefaultFrameSpec)
    with pytest.raises(
        HostValidationError, match=r"host 'e': .*builds a EmbeddedHost, which supplies no"
    ):
        route({"ip": "192.0.2.1", "os_type": "t-default-frame"})
    route({"ip": "192.0.2.1", "os_type": "t-default-frame", "command_frame": "zephyr"})


@dataclasses.dataclass(slots=True, kw_only=True)
class _Dockerish(UnixHost):
    docker_capable: bool = True


def test_docker_priority_reads_the_class_s_declared_docker_capable():
    register_host_class("t-dockerish", _Dockerish)
    entry = {"ip": "10.0.0.1", "docker_priority": 5}
    assert host_identity({**entry, "os_type": "t-dockerish"}, Element("e")).docker_capable is True
    validate_host_dict({**entry, "os_type": "t-dockerish"}, element=Element("e"))
    with pytest.raises(
        HostValidationError, match=r"docker_priority on .* which is not docker_capable"
    ):
        validate_host_dict(dict(entry), element=Element("e"))


class _EmbeddedOnly(LinkImpairer):
    host_families: ClassVar[frozenset[str]] = frozenset({"embedded"})


def test_an_impairer_of_the_wrong_family_fails_in_both_modes():
    register_impairer("t-embedded-only", _EmbeddedOnly)
    entry = {"ip": "1.1.1.1", "valid_impairers": ["netem", "t-embedded-only"]}
    UnixHostSpec.model_validate(entry)  # structural: membership only
    with pytest.raises(
        HostValidationError, match=r"impairer 't-embedded-only' is not valid on a unix host"
    ):
        validate_host_dict(dict(entry), element=Element("e"))
    with pytest.raises(HostValidationError, match=r"impairer 't-embedded-only' is not valid"):
        create_host_from_dict(dict(entry), element=Element("e"))


@pytest.mark.parametrize("route", _ROUTES.values(), ids=_ROUTES.keys())
def test_a_replaced_stock_impairer_is_checked_under_the_omitted_menu(route):
    """The stock menu skips the check only while "netem" is still otto's own registration."""
    register_impairer("netem", _EmbeddedOnly, overwrite=True)
    with pytest.raises(
        HostValidationError, match=r"host 'e': impairer 'netem' is not valid on a unix host"
    ):
        route({"ip": "1.1.1.1"})


def test_an_explicit_stock_impairer_menu_still_validates():
    """An entry's own ["netem"] is checked like any menu: netem serves unix hosts."""
    validate_host_dict({"ip": "1.1.1.1", "valid_impairers": ["netem"]}, element=Element("e"))


class _EmbeddedOnlyDefaultSpec(UnixHostSpec):
    """A custom spec whose own DEFAULT impairer menu serves only embedded hosts."""

    valid_impairers: list[str] = Field(default_factory=lambda: ["t-embedded-only"])


@dataclasses.dataclass(slots=True, kw_only=True)
class _EmbeddedOnlyDefaultHost(UnixHost):
    pass


@pytest.mark.parametrize("route", _ROUTES.values(), ids=_ROUTES.keys())
def test_a_custom_spec_s_default_impairer_menu_is_checked_too(route):
    """Only otto's stock default menu goes unchecked; a custom spec's own default is lab data."""
    register_impairer("t-embedded-only", _EmbeddedOnly)
    register_host_class("t-emb-default", _EmbeddedOnlyDefaultHost, spec=_EmbeddedOnlyDefaultSpec)
    with pytest.raises(
        HostValidationError, match=r"impairer 't-embedded-only' is not valid on a unix host"
    ):
        route({"ip": "1.1.1.1", "os_type": "t-emb-default"})


class _LeakySpec(UnixHostSpec):
    """A contextual check that fails with a ValidationError over a secret input."""

    def _contextual_checks(self, ctx):
        del ctx
        TypeAdapter(int).validate_python("s3cret-tail")


@dataclasses.dataclass(slots=True, kw_only=True)
class _LeakyHost(UnixHost):
    pass


def test_a_contextual_validation_error_never_echoes_its_input():
    register_host_class("t-leaky", _LeakyHost, spec=_LeakySpec)
    with pytest.raises(HostValidationError, match=r"^host 'e': ") as exc:
        validate_host_dict({"ip": "10.0.0.1", "os_type": "t-leaky"}, element=Element("e"))
    assert "s3cret" not in str(exc.value)
