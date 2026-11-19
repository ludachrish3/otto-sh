"""The netbox backend against a local stub — the real pynetbox client, no network (spec §9.2, §14).

Every match= below is anchored on the phrase the guard exists for, never on a
bare word a repr or a locals dump could satisfy.
"""

import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from otto.inventory import (
    InventoryConstructionError,
    InventoryEnv,
    InventoryError,
    InventoryKeyError,
)
from otto.inventory.config import compile_inventory, construct_inventory
from otto.inventory.netbox import NATIVE_SUPPLIES, _NetBoxInventory
from otto.inventory.registry import INVENTORY_BACKENDS
from otto.models.settings import InventoryConfigSpec
from otto.testing import assert_inventory_conforms

from .netbox_stub import TOKEN, NetBoxStub, device, self_signed_cert, virtual_machine

# A port nothing listens on: `otto` never opens one, and 9 is the stdlib
# discard service, so a connect() here is refused rather than answered.
DEAD = "http://127.0.0.1:9"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("NETBOX_TOKEN", TOKEN)


def _inv(stub, **kw):
    return _NetBoxInventory(url=stub.base, **kw)


def test_netbox_is_registered_and_construction_is_lazy(tmp_path):
    prepared = INVENTORY_BACKENDS.prepare(
        name="netbox", raw={"url": DEAD}, env=InventoryEnv(tmp_path, "o.toml")
    )
    inv = INVENTORY_BACKENDS.build(prepared=prepared)  # nothing listens; no error until first use
    assert isinstance(inv, _NetBoxInventory)
    assert inv.label == f"netbox:{DEAD}"
    assert inv.supplies == NATIVE_SUPPLIES
    with pytest.raises(InventoryError, match=f"netbox inventory {DEAD}"):
        inv.list_keys()


def test_construction_makes_no_requests_and_the_first_use_fetches_once():
    with NetBoxStub([device(1, "d1"), device(2, "d2")], page_size=1) as stub:
        inv = _inv(stub)
        assert stub.requests == []  # a lab with no referenced entry never talks to NetBox
        assert inv.list_keys() == ["d1", "d2"]
        after_first = list(stub.requests)
        assert after_first, "the first use must actually fetch"
        # Two pages of one device each -> two device calls, and every request
        # went to the devices endpoint (no per-device round trip).
        assert [r.split("?")[0] for r in after_first] == ["/api/dcim/devices/"] * 2
        inv.lookup("d1")
        inv.lookup("d2")
        inv.list_keys()
        assert stub.requests == after_first, "the set is fetched once for the object's lifetime"


def test_token_env_is_read_at_first_use_not_at_construction(monkeypatch):
    monkeypatch.delenv("NETBOX_TOKEN")
    inv = _NetBoxInventory(url=DEAD)  # construction is silent even with no token anywhere
    with pytest.raises(
        InventoryError, match="environment variable 'NETBOX_TOKEN' is not set"
    ) as excinfo:
        inv.list_keys()
    assert "the API token never sits in a settings file" in str(excinfo.value)


def test_token_env_names_the_variable_the_token_is_read_from(monkeypatch):
    monkeypatch.delenv("NETBOX_TOKEN")
    monkeypatch.setenv("NB_TOK", TOKEN)
    with NetBoxStub([device(1, "d1")]) as stub:
        # The stub 403s on a wrong token, so this passing proves NB_TOK's VALUE
        # reached the Authorization header, not merely that it was read.
        assert _inv(stub, token_env="NB_TOK").list_keys() == ["d1"]


def test_mapping_row_by_row_and_pagination():
    devices = [
        device(1, "d1", custom_fields={"unrelated": "x"}),
        device(2, "d2", rack=None, position=None, platform=None),
        device(3, "d3"),
    ]
    with NetBoxStub(devices, page_size=2) as stub:
        inv = _inv(stub)
        assert inv.list_keys() == ["d1", "d2", "d3"]  # three devices over two pages
        r = inv.lookup("d1")
        assert r.ip == "10.0.0.1"  # prefix length stripped
        assert (r.site, r.rack, r.shelf, r.board, r.os_name, r.is_virtual) == (
            "lab-a",
            1,
            3,
            "cx-4",
            "Ubuntu",
            False,
        )
        assert r.extra == {
            "id": 1,
            "serial": "S1",
            "asset_tag": "A-1",
            "status": "active",
            "tags": ["lab"],
        }
        # The unrelated custom field never entered the record.
        assert r.model_fields_set <= NATIVE_SUPPLIES | {"extra"}
        r2 = inv.lookup("d2")
        assert (r2.rack, r2.shelf, r2.os_name) == (None, None, None)
        assert inv.fingerprint() is None
        assert_inventory_conforms(inv, expected_keys=["d1", "d2", "d3"])


def test_filter_is_forwarded():
    with NetBoxStub([device(1, "d1")]) as stub:
        _inv(stub, filter={"site": "lab-a", "status": "active"}).list_keys()
        assert stub.queries[0]["site"] == ["lab-a"]
        assert stub.queries[0]["status"] == ["active"]


def test_mapped_custom_fields_join_supplies_and_wrong_type_names_device_and_field():
    devices = [device(1, "d1", custom_fields={"sw_version": "4.2", "element_id": "not-an-int"})]
    with NetBoxStub(devices) as stub:
        inv = _inv(stub, custom_fields={"sw_version": "sw_version"})
        assert inv.supplies == NATIVE_SUPPLIES | {"sw_version"}
        assert inv.lookup("d1").sw_version == "4.2"
        bad = _inv(stub, custom_fields={"element_id": "element_id"})
        with pytest.raises(InventoryError, match=r"device 'd1' \(id 1\).*element_id") as excinfo:
            bad.lookup("d1")
        # The field-level message reaches the user as-is: the per-device guard
        # must not re-wrap an error that already names the URL and the device.
        assert str(excinfo.value).count("netbox inventory") == 1


def test_custom_fields_may_only_map_a_record_field():
    with pytest.raises(
        ValueError, match="custom_fields maps 'bogus', which is not an inventory record field"
    ):
        _NetBoxInventory(url=DEAD, custom_fields={"bogus": "cf"})


def test_extra_custom_fields_are_opt_in():
    with NetBoxStub([device(1, "d1", custom_fields={"owner": "ops"})]) as stub:
        assert "owner" not in _inv(stub).lookup("d1").extra
        assert _inv(stub, extra_custom_fields=["owner"]).lookup("d1").extra["owner"] == "ops"


@pytest.mark.parametrize(
    ("ip_source", "expected"), [("oob_ip", "10.9.9.9"), ("cf:mgmt", "10.8.8.8")]
)
def test_ip_source_variants(ip_source, expected):
    d = device(
        1, "d1", ip="10.0.0.1/24", oob_ip="10.9.9.9/32", custom_fields={"mgmt": "10.8.8.8/24"}
    )
    with NetBoxStub([d]) as stub:
        assert _inv(stub, ip_source=ip_source).lookup("d1").ip == expected


def test_a_custom_field_address_that_is_not_an_address_is_refused_naming_the_field():
    """A NetBox editor's custom field must never reach a command on a hop."""
    d = device(1, "d1", custom_fields={"mgmt": "1.2.3.4'; touch /tmp/pwned; '"})
    with (
        NetBoxStub([d]) as stub,
        pytest.raises(
            InventoryError,
            match=r"device 'd1' \(id 1\): ip: .* \(ip read from ip_source 'cf:mgmt'\)",
        ),
    ):
        _inv(stub, ip_source="cf:mgmt").list_keys()


def test_ip_source_must_name_a_source_the_backend_can_read():
    with pytest.raises(
        ValueError,
        match=r"ip_source must be 'primary_ip4', 'oob_ip', or 'cf:<custom field>', got 'bogus'",
    ):
        _NetBoxInventory(url=DEAD, ip_source="bogus")
    with pytest.raises(ValueError, match="got 'cf:'"):
        _NetBoxInventory(url=DEAD, ip_source="cf:")  # a prefix with no field name


def test_device_without_an_address_fails_only_when_looked_up():
    with NetBoxStub([device(1, "noip", ip=None), device(2, "ok")]) as stub:
        inv = _inv(stub)
        assert inv.list_keys() == ["ok"]
        assert inv.lookup("ok").ip == "10.0.0.1"
        with pytest.raises(
            InventoryError,
            match=r"device 'noip' \(id 1\) has no address at ip_source 'primary_ip4'",
        ):
            inv.lookup("noip")


def test_duplicate_names_name_both_ids():
    with NetBoxStub([device(1, "dup"), device(2, "dup")]) as stub:
        with pytest.raises(
            InventoryError, match=r"duplicate device name 'dup' \(device ids 1 and 2\)"
        ) as excinfo:
            _inv(stub).list_keys()
        # EXACT, not a substring: an error that already names the URL and the
        # device must pass through the per-device guard untouched, not come
        # back wrapped in a second "netbox inventory ...: device ...:" prefix.
        assert str(excinfo.value) == (
            f"netbox inventory {stub.base}: duplicate device name 'dup' (device ids 1 and 2)"
        )


def test_auth_failure_is_an_inventory_error_naming_the_url(monkeypatch):
    monkeypatch.setenv("NETBOX_TOKEN", "wrong")
    # Not combinable: the inner match= reads `stub.base`, which only the outer
    # `with` binds.
    with NetBoxStub([device(1, "d1")]) as stub:  # noqa: SIM117
        with pytest.raises(InventoryError, match=f"netbox inventory {stub.base}"):
            _inv(stub).list_keys()


def test_unknown_key():
    with (
        NetBoxStub([device(1, "d1")]) as stub,
        pytest.raises(
            InventoryKeyError, match="inventory key 'zz' not found in inventory 'netbox:"
        ),
    ):
        _inv(stub).lookup("zz")


def test_url_must_be_non_empty():
    with pytest.raises(ValueError, match="'url' must be a non-empty NetBox base URL"):
        _NetBoxInventory(url="")


def test_the_default_timeout_is_thirty_seconds():
    """Generous, because a filtered fetch over a large instance is a real query."""
    assert _NetBoxInventory(url=DEAD).timeout == 30.0


@pytest.mark.parametrize("bad", [0, -1, "30", None, True])
def test_a_timeout_that_is_not_a_positive_number_is_refused_at_construction(bad):
    """``True`` is in here on purpose: ``bool`` is an ``int``, and ``timeout = true``
    in a TOML table would otherwise quietly mean "one second"."""
    with pytest.raises(ValueError, match="timeout must be a positive number of seconds"):
        _NetBoxInventory(url=DEAD, timeout=bad)


def _compiled(tmp_path, table, *, anchor=None):
    """The ``[inventory]`` table prepared as a project's settings file would declare it."""
    return compile_inventory(
        cfg=InventoryConfigSpec.model_validate({"backend": "netbox", "cache_ttl": "0", **table}),
        anchor_dir=anchor or tmp_path,
        origin=str(tmp_path / ".otto" / "settings.toml"),
    )


def test_a_refused_timeout_names_the_settings_file_and_the_backend(tmp_path):
    """The construction stage turns the ValueError into an error the user can act on."""
    with pytest.raises(
        InventoryConstructionError,
        match=r"'netbox' \(registered by .*; configured in .*settings\.toml\): "
        r"construction failed: ValueError: timeout must be a positive number of seconds",
    ):
        construct_inventory(compiled=_compiled(tmp_path, {"url": DEAD, "timeout": 0}))


def test_two_projects_declaring_one_table_build_identical_inventories(tmp_path):
    # Nothing here interprets a path, so two projects declaring the same table
    # build an inventory identical in EVERY attribute — the whole `vars()`,
    # not a chosen pair, because `CompiledInventory.same_as` ignores
    # anchor_dir and an attribute that quietly remembered it would make two
    # such projects disagree about an inventory they call the same.
    a = construct_inventory(compiled=_compiled(tmp_path, {"url": DEAD}, anchor=tmp_path / "proj-a"))
    b = construct_inventory(compiled=_compiled(tmp_path, {"url": DEAD}, anchor=tmp_path / "proj-b"))
    assert vars(a) == vars(b)
    assert not [v for v in vars(a).values() if isinstance(v, Path)]


def test_a_bad_value_names_the_settings_file_and_the_backend(tmp_path):
    # Why the constructor raises ValueError rather than InventoryError: the
    # registry's construction stage wraps it with the origin the user must edit.
    with pytest.raises(
        InventoryConstructionError, match=r"'netbox' \(.*settings\.toml\): construction failed"
    ):
        construct_inventory(compiled=_compiled(tmp_path, {"url": DEAD, "ip_source": "bogus"}))


def test_an_unknown_key_names_the_settings_file_and_the_backend(tmp_path):
    with pytest.raises(
        InventoryConstructionError,
        match=r"'netbox' \(.*settings\.toml\): parse failed: urll: Extra inputs",
    ):
        _compiled(tmp_path, {"url": DEAD, "urll": "typo"})


# -- round 1: the wire-shape seam ------------------------------------------


def test_verify_is_no_longer_an_argument():
    # One source of trust — the OS store. A bundle path or an off switch in a
    # COMMITTED settings table is exactly the proliferation the spec removes,
    # and the constructor must not quietly grow it back.
    with pytest.raises(TypeError, match="verify"):
        _NetBoxInventory(url=DEAD, verify=False)  # type: ignore[call-arg]


def test_a_certificate_the_os_store_does_not_know_is_refused(tmp_path, monkeypatch):
    # The stub speaks real TLS with a self-signed certificate, so there is
    # something to actually verify — over plain HTTP the trust path is inert
    # and deleting it entirely would leave every other test green.
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    # Not combinable: the inner match= reads `stub.base`, which only the outer
    # `with` binds.
    with NetBoxStub([device(1, "d1")], tls=self_signed_cert(tmp_path)) as stub:  # noqa: SIM117
        with pytest.raises(InventoryError, match=f"netbox inventory {stub.base}: SSLError"):
            _inv(stub).list_keys()


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="SSL_CERT_FILE is an OpenSSL override; truststore reads the platform store"
    " on macOS/Windows",
)
def test_a_certificate_the_os_store_trusts_is_accepted(tmp_path, monkeypatch):
    """The guard that the OS-store path is REAL.

    ``SSL_CERT_FILE`` is OpenSSL's own override of the default trust file;
    truststore reads the defaults at socket-wrap time, so setting it here,
    before the fetch, is what "install the CA in the OS store" looks like
    inside a test. With certifi still in play this same fetch fails — that is
    the whole discriminator.
    """
    certfile, keyfile = self_signed_cert(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    with NetBoxStub([device(1, "d1")], tls=(certfile, keyfile)) as stub:
        assert _inv(stub).list_keys() == ["d1"]


def test_the_fetch_uses_the_shared_os_trust_session(monkeypatch):
    """Trust and the timeout bound come from ``otto.tls``, not a private copy here.

    Observed at the one seam the backend owns: the session it hands pynetbox,
    AND that the configured timeout actually reaches every request the
    backend sends through it — the factory argument alone would pass even if
    the value were dropped on the way to ``HTTPAdapter.send``.
    """
    from requests.adapters import HTTPAdapter

    import otto.inventory.netbox as netbox_module
    from otto import tls

    handed: list = []
    real_factory = tls.os_trust_session

    def spy(*, timeout):
        session = real_factory(timeout=timeout)
        handed.append((timeout, session))
        return session

    monkeypatch.setattr(netbox_module, "os_trust_session", spy)

    timeouts_seen: list = []
    real_send = HTTPAdapter.send

    def send_spy(self, request, **kwargs):
        timeouts_seen.append(kwargs["timeout"])
        return real_send(self, request, **kwargs)

    monkeypatch.setattr(HTTPAdapter, "send", send_spy)

    with NetBoxStub([device(1, "d1")]) as stub:
        assert _inv(stub, timeout=0.3).list_keys() == ["d1"]
    assert [t for t, _ in handed] == [0.3]
    assert timeouts_seen, "no request was observed through HTTPAdapter.send"
    assert all(t == 0.3 for t in timeouts_seen)


def _watch_session(monkeypatch):
    """Return the path of every request the fetch's own session sends, then ``"closed"``.

    Observed on the session :func:`otto.tls.os_trust_session` hands pynetbox,
    so a detail GET pynetbox makes for a field the list response lacked shows
    up here, and the order says whether it went out while the session was
    still open.
    """
    import otto.inventory.netbox as netbox_module

    events: list[str] = []
    real_factory = netbox_module.os_trust_session

    def factory(*, timeout):
        session = real_factory(timeout=timeout)
        real_send, real_close = session.send, session.close

        def send(request, **kwargs):
            events.append(urlsplit(request.url).path)
            return real_send(request, **kwargs)

        def close():
            events.append("closed")
            real_close()

        session.send = send
        session.close = close
        return session

    monkeypatch.setattr(netbox_module, "os_trust_session", factory)
    return events


def _paths(stub):
    return [r.split("?")[0] for r in stub.requests]


def test_a_full_list_response_costs_no_detail_request(monkeypatch):
    events = _watch_session(monkeypatch)
    devices = [device(1, "d1"), device(2, "d2", ip="10.0.0.2/24")]
    with NetBoxStub(devices, virtual_machines=[virtual_machine(7, "vm1")]) as stub:
        assert _inv(stub, virtual_machines=True).list_keys() == ["d1", "d2", "vm1"]
        assert _paths(stub) == ["/api/dcim/devices/", _VMS_PATH]
    assert events == ["/api/dcim/devices/", _VMS_PATH, "closed"]


def test_a_device_missing_a_field_names_the_device_and_the_url(monkeypatch):
    # An older or plugin-trimmed serializer omits `position`; pynetbox looks
    # for it on the device's detail page (inside the session), and when that
    # lacks it too the error must name the device and the URL, never escape
    # as a bare AttributeError.
    events = _watch_session(monkeypatch)
    with NetBoxStub([device(1, "d1", drop=("position",))]) as stub:
        with pytest.raises(
            InventoryError,
            match=(
                rf"netbox inventory {stub.base}: device 'd1' \(id 1\): "
                r'AttributeError: object has no attribute "position"$'
            ),
        ):
            _inv(stub).list_keys()
        assert _paths(stub) == ["/api/dcim/devices/", "/api/dcim/devices/1/"]
    assert events == ["/api/dcim/devices/", "/api/dcim/devices/1/", "closed"]


def test_a_device_missing_two_fields_names_the_one_read_first():
    # Device fields are read in one fixed order: `site` before `rack`.
    with NetBoxStub([device(1, "d1", drop=("site", "rack"))]) as stub:  # noqa: SIM117
        with pytest.raises(
            InventoryError, match=r'AttributeError: object has no attribute "site"$'
        ):
            _inv(stub).list_keys()


def test_a_device_missing_serial_and_status_names_serial_first():
    # `extra` is read in main's order: id, serial, asset_tag, status, tags.
    with NetBoxStub([device(1, "d1", drop=("serial", "status"))]) as stub:  # noqa: SIM117
        with pytest.raises(
            InventoryError, match=r'AttributeError: object has no attribute "serial"$'
        ):
            _inv(stub).list_keys()


def test_a_device_with_no_name_anywhere_is_an_unnamed_device():
    # Absent from the list AND the detail page: unnamed, skipped and counted.
    with NetBoxStub([device(1, "d1", drop=("name",)), device(2, "d2")]) as stub:
        inv = _inv(stub)
        assert inv.list_keys() == ["d2"]
        assert inv.unnamed_device_ids == [1]
        assert _paths(stub) == ["/api/dcim/devices/", "/api/dcim/devices/1/"]


def test_a_device_whose_netbox_has_no_oob_ip_is_addressless():
    # NetBox before 3.6 has no device `oob_ip` at all: the device is selected
    # and skipped as addressless, exactly as a null `oob_ip` is. The cost,
    # pinned so it stays visible: one detail GET per device (the list lacked
    # the field, so its detail page is asked), then the skip.
    with NetBoxStub([device(1, "d1", drop=("oob_ip",))]) as stub:
        inv = _inv(stub, ip_source="oob_ip")
        assert inv.list_keys() == []
        assert inv.addressless_device_names == ["d1"]
        assert _paths(stub) == ["/api/dcim/devices/", "/api/dcim/devices/1/"]
        with pytest.raises(InventoryError) as excinfo:
            inv.lookup("d1")
        assert str(excinfo.value) == (
            f"netbox inventory {stub.base}: device 'd1' (id 1) has no address at ip_source 'oob_ip'"
        )


@pytest.mark.parametrize(
    ("trim", "ip_source"),
    [
        ({"fields": "id,url,primary_ip4"}, "primary_ip4"),  # no name in the list
        ({"omit": "site"}, "primary_ip4"),
        ({"brief": True}, "cf:mgmt"),  # brief has no custom_fields
    ],
    ids=["fields", "omit", "brief"],
)
def test_a_trimmed_device_list_is_completed_from_the_detail_pages(monkeypatch, trim, ip_source):
    """What NetBox trimmed from the list, pynetbox reads from each detail page, in session."""
    devices = [
        device(1, "d1", custom_fields={"mgmt": "10.8.8.1/24"}),
        device(2, "d2", ip="10.0.0.2/24", custom_fields={"mgmt": "10.8.8.2/24"}),
    ]
    with NetBoxStub(devices) as stub:
        full = {k: _inv(stub, ip_source=ip_source).lookup(k) for k in ("d1", "d2")}
        stub.requests.clear()
        events = _watch_session(monkeypatch)
        inv = _inv(stub, filter=trim, ip_source=ip_source)
        assert inv.list_keys() == ["d1", "d2"]
        assert {k: inv.lookup(k) for k in ("d1", "d2")} == full
        details = ["/api/dcim/devices/1/", "/api/dcim/devices/2/"]
        assert _paths(stub) == ["/api/dcim/devices/", *details]
        assert inv.filter == trim  # forwarded verbatim
    assert events == ["/api/dcim/devices/", *details, "closed"]


def test_a_brief_virtual_machine_gets_its_address_from_the_detail_page(monkeypatch):
    # pynetbox declares `primary_ip4` on its VirtualMachines model, so reading
    # it off a brief record returns that class, not a lazy load: without an
    # explicit detail load every brief VM would be "addressless".
    events = _watch_session(monkeypatch)
    vms = [virtual_machine(7, "vm1", ip="10.1.0.7/24")]
    with NetBoxStub([device(1, "d1")], virtual_machines=vms) as stub:
        inv = _inv(stub, virtual_machines=True, vm_filter={"brief": True})
        assert inv.list_keys() == ["d1", "vm1"]
        assert inv.lookup("vm1").ip == "10.1.0.7"
        detail = f"{_VMS_PATH}7/"
        assert _paths(stub) == ["/api/dcim/devices/", _VMS_PATH, detail]
    assert events == ["/api/dcim/devices/", _VMS_PATH, detail, "closed"]


def test_an_explicitly_null_virtual_machine_address_skips_with_no_detail_request():
    with NetBoxStub([], virtual_machines=[virtual_machine(7, "vm1", ip=None)]) as stub:
        inv = _inv(stub, virtual_machines=True)
        assert inv.list_keys() == []
        assert inv.addressless_virtual_machine_names == ["vm1"]
        assert _paths(stub) == ["/api/dcim/devices/", _VMS_PATH]


@pytest.mark.parametrize(
    "trim", [{"brief": True}, {"fields": "url,name"}], ids=["brief", "fields-url-name"]
)
def test_a_device_whose_list_omitted_its_address_gets_it_from_the_detail_page(monkeypatch, trim):
    # A behaviour fix over main, where the omitted `primary_ip4` read as the
    # pynetbox model class and the device silently dropped out as addressless.
    events = _watch_session(monkeypatch)
    with NetBoxStub([device(1, "d1", ip="10.0.0.9/24")]) as stub:
        inv = _inv(stub, filter=trim)
        assert inv.list_keys() == ["d1"]
        assert inv.lookup("d1").ip == "10.0.0.9"
        assert inv.addressless_device_names == []
        assert _paths(stub) == ["/api/dcim/devices/", "/api/dcim/devices/1/"]
    assert events == ["/api/dcim/devices/", "/api/dcim/devices/1/", "closed"]


@pytest.mark.parametrize("kind", ["device", "virtual machine"])
def test_a_projection_without_url_is_completed_through_the_listing_endpoint(monkeypatch, kind):
    # `fields = "id,name"` leaves no detail URL on the record: the object is
    # fetched by id from the endpoint it was listed from, once, in session.
    events = _watch_session(monkeypatch)
    trim = {"fields": "id,name"}
    if kind == "device":
        stub_kw = {"devices": [device(3, "d3", ip="10.0.0.3/24")]}
        inv_kw = {"filter": trim}
        listing, key, ip = "/api/dcim/devices/", "d3", "10.0.0.3"
    else:
        stub_kw = {"devices": [], "virtual_machines": [virtual_machine(7, "vm1", ip="10.1.0.7/24")]}
        inv_kw = {"virtual_machines": True, "vm_filter": trim}
        listing, key, ip = _VMS_PATH, "vm1", "10.1.0.7"
    detail = f"{listing}{3 if kind == 'device' else 7}/"
    with NetBoxStub(**stub_kw) as stub:
        inv = _inv(stub, **inv_kw)
        assert inv.list_keys() == [key]
        assert inv.lookup(key).ip == ip
        lists = ["/api/dcim/devices/"] + ([_VMS_PATH] if kind != "device" else [])
        assert _paths(stub) == [*lists, detail]
    assert events == [*lists, detail, "closed"]


def test_a_projection_with_neither_url_nor_id_fails_naming_the_field():
    # Nothing to load the object by: never a silent addressless skip.
    with NetBoxStub([device(1, "d1")]) as stub:
        with pytest.raises(InventoryError) as excinfo:
            _inv(stub, filter={"fields": "name"}).list_keys()
        assert str(excinfo.value) == (
            f"netbox inventory {stub.base}: device 'd1' (id ?): AttributeError: object has no "
            'attribute "id", and neither a url nor an id to load it by'
        )
        assert _paths(stub) == ["/api/dcim/devices/"]


def test_an_object_renamed_on_its_detail_page_is_keyed_by_the_detail_name(monkeypatch):
    # Listed as `listed`, its detail page (loaded for the address the list
    # lacked) names it `d`: the key, the duplicate check and the record all
    # come from the completed object, with no second detail GET.
    events = _watch_session(monkeypatch)
    vms = [virtual_machine(7, "listed", ip="10.1.0.7/24")]
    with NetBoxStub([device(1, "d")], virtual_machines=vms) as stub:
        detail = f"{_VMS_PATH}7/"
        stub.detail_overrides = {detail: {"name": "d"}}
        inv = _inv(stub, virtual_machines=True, vm_filter={"fields": "id,url,name"})
        with pytest.raises(InventoryError) as excinfo:
            inv.list_keys()
        assert str(excinfo.value) == (
            f"netbox inventory {stub.base}: duplicate name 'd' "
            "(device id 1 and virtual machine id 7)"
        )
        assert _paths(stub) == ["/api/dcim/devices/", _VMS_PATH, detail]
    assert events == ["/api/dcim/devices/", _VMS_PATH, detail, "closed"]


def test_a_value_the_detail_page_corrects_is_never_validated_from_the_list():
    # The listing's `mgmt` is garbage, its detail page (loaded for the site
    # the projection left out) holds the real address: the record is read
    # from the completed object only, and nothing from the list survives it.
    vms = [virtual_machine(7, "vm1", custom_fields={"mgmt": "not an ip"})]
    with NetBoxStub([device(1, "d1")], virtual_machines=vms) as stub:
        detail = f"{_VMS_PATH}7/"
        stub.detail_overrides = {detail: {"custom_fields": {"mgmt": "10.0.0.7/24"}}}
        inv = _inv(
            stub,
            virtual_machines=True,
            vm_filter={"fields": "id,url,name,custom_fields"},
            vm_ip_source="cf:mgmt",
        )
        assert inv.list_keys() == ["d1", "vm1"]
        assert inv.lookup("vm1").ip == "10.0.0.7"
        assert _paths(stub) == ["/api/dcim/devices/", _VMS_PATH, detail]


def test_a_duplicate_is_reported_before_the_second_objects_record_is_validated():
    # As on main: the name is the first thing known about an object, so a
    # duplicate wins over whatever else is wrong with the second one.
    devices = [device(1, "d"), device(2, "d", custom_fields={"slot": "not-a-number"})]
    with NetBoxStub(devices) as stub:
        with pytest.raises(InventoryError) as excinfo:
            _inv(stub, custom_fields={"shelf": "slot"}).list_keys()
        assert str(excinfo.value) == (
            f"netbox inventory {stub.base}: duplicate device name 'd' (device ids 1 and 2)"
        )


def test_a_renamed_object_is_published_under_its_detail_name():
    vms = [virtual_machine(7, "listed", ip="10.1.0.7/24")]
    with NetBoxStub([device(1, "d1")], virtual_machines=vms) as stub:
        stub.detail_overrides = {f"{_VMS_PATH}7/": {"name": "vm-renamed"}}
        inv = _inv(stub, virtual_machines=True, vm_filter={"fields": "id,url,name"})
        assert inv.list_keys() == ["d1", "vm-renamed"]
        assert inv.lookup("vm-renamed").ip == "10.1.0.7"


@pytest.mark.parametrize(
    ("trim", "named", "error"),
    [
        # By id, through the endpoint it was listed from: pynetbox says None.
        (
            {"brief": True},
            "device 'd1' (id 1)",
            "LookupError: its detail page (id 1) does not exist",
        ),
        # No id to load by, only the url: pynetbox's own 404 error.
        ({"fields": "url,name"}, "device 'd1' (id ?)", "RequestError: "),
    ],
    ids=["by-id", "by-url"],
)
def test_a_detail_page_that_404s_is_one_attempt_and_names_the_device(trim, named, error):
    with NetBoxStub([device(1, "d1")]) as stub:
        stub.detail_missing = {1}
        with pytest.raises(InventoryError) as excinfo:
            _inv(stub, filter=trim).list_keys()
        assert str(excinfo.value).startswith(f"netbox inventory {stub.base}: {named}: {error}")
        assert _paths(stub) == ["/api/dcim/devices/", "/api/dcim/devices/1/"]


@pytest.mark.parametrize(
    ("fields", "keys", "addressless"),
    [
        # By id (`endpoint.get`), a fresh record: the detail page's alone, so
        # the device is addressless, never published at the list's address.
        ("id,url,name,primary_ip4", [], ["d1"]),
        ("id,name,primary_ip4", [], ["d1"]),
        # No id, only a url: pynetbox's `full_details()` updates in place, so
        # the list's address survives the detail page omitting it.
        ("url,name,primary_ip4", ["d1"], []),
    ],
    ids=["id-and-url", "id-only", "url-only"],
)
def test_a_field_the_detail_page_leaves_out_follows_the_librarys_load(fields, keys, addressless):
    with NetBoxStub([device(1, "d1")]) as stub:
        stub.detail_drops = {"/api/dcim/devices/1/": {"primary_ip4"}}
        inv = _inv(stub, filter={"fields": fields})
        assert inv.list_keys() == keys
        assert inv.addressless_device_names == addressless
        assert _paths(stub) == ["/api/dcim/devices/", "/api/dcim/devices/1/"]


def test_an_object_the_listing_already_skips_is_never_completed():
    # Explicitly addressless and unnamed, both settled by what the list
    # served: skipped after the one list request, as before the detail load
    # existed, even though neither detail page would answer.
    devices = [device(1, "d1", ip=None), device(2, None)]
    with NetBoxStub(devices) as stub:
        stub.detail_missing = {1, 2}
        inv = _inv(stub, filter={"fields": "id,name,primary_ip4"})
        assert inv.list_keys() == []
        assert inv.addressless_device_names == ["d1"]
        assert inv.unnamed_device_ids == [2]
        assert _paths(stub) == ["/api/dcim/devices/"]


@pytest.mark.parametrize("filter_", [{"fields": "id,name"}, {"brief": True}])
def test_an_unnamed_device_is_skipped_without_its_address_field(filter_):
    # Unnamed as listed settles the skip on its own: no address field needed,
    # so no detail request, as before the detail load existed.
    with NetBoxStub([device(1, None), device(2, None)]) as stub:
        stub.detail_missing = {1, 2}
        inv = _inv(stub, filter=filter_)
        assert inv.list_keys() == []
        assert inv.unnamed_device_ids == [1, 2]
        assert _paths(stub) == ["/api/dcim/devices/"]


def test_a_trimmed_virtual_machine_list_is_completed_from_the_detail_pages(monkeypatch):
    vms = [virtual_machine(7, "vm1", custom_fields={"mgmt": "10.8.8.9/24"})]
    events = _watch_session(monkeypatch)
    with NetBoxStub([device(1, "d1")], virtual_machines=vms) as stub:
        inv = _inv(
            stub,
            virtual_machines=True,
            vm_filter={"fields": "id,url,primary_ip4"},
            vm_ip_source="cf:mgmt",
        )
        assert inv.list_keys() == ["d1", "vm1"]
        r = inv.lookup("vm1")
        assert (r.ip, r.site, r.os_name, r.is_virtual) == ("10.8.8.9", "lab-a", "Ubuntu", True)
        assert r.extra == {"id": 7, "cluster": "vmc-1", "status": "active", "tags": ["lab"]}
        assert stub.vm_queries[0]["fields"] == ["id,url,primary_ip4"]  # forwarded verbatim
        detail = f"{_VMS_PATH}7/"
        assert _paths(stub) == ["/api/dcim/devices/", _VMS_PATH, detail]
    assert events == ["/api/dcim/devices/", _VMS_PATH, detail, "closed"]


def test_the_serialization_schema_is_the_model_s():
    """Leaving keys out at their default must not cost the model its output schema."""
    from otto.inventory.config import NetBoxInventoryConfig

    schema = NetBoxInventoryConfig.model_json_schema(mode="serialization")
    assert schema["required"] == ["url"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(NetBoxInventoryConfig.model_fields)
    for name in ("virtual_machines", "vm_filter", "vm_ip_source"):
        assert "default" in schema["properties"][name], name


def test_a_device_only_fetch_failure_names_no_listing():
    # The device-only text is what it always was: no "listing devices:" step.
    with NetBoxStub([device(1, "d1")], forbidden={"/api/dcim/devices/"}) as stub:
        with pytest.raises(InventoryError) as excinfo:
            _inv(stub).list_keys()
        assert str(excinfo.value).startswith(f"netbox inventory {stub.base}: RequestError: ")


def test_a_half_u_position_string_becomes_the_integer_shelf():
    # NetBox's `position` is a DecimalField serialised as a string, and half-U
    # positions are ordinary; `int("3.5")` would raise.
    devices = [device(1, "half", position="3.5"), device(2, "none", position=None)]
    with NetBoxStub(devices) as stub:
        inv = _inv(stub)
        assert inv.lookup("half").shelf == 3
        assert inv.lookup("none").shelf is None


def test_unnamed_devices_are_skipped_and_counted():
    # `name` is nullable in NetBox and the default filter is "everything":
    # keyed by name these would all collide on the literal string 'None'.
    devices = [device(1, None), device(2, "named"), device(3, "")]
    with NetBoxStub(devices) as stub:
        inv = _inv(stub)
        assert inv.list_keys() == ["named"]
        assert inv.unnamed_device_ids == [1, 3]
        with pytest.raises(InventoryKeyError, match="inventory key 'None' not found"):
            inv.lookup("None")


def test_unnamed_device_ids_is_empty_before_the_first_fetch():
    assert _NetBoxInventory(url=DEAD).unnamed_device_ids == []


@pytest.mark.parametrize("field", ["ip", "creds", "interfaces"])
def test_custom_fields_may_not_map_a_field_the_backend_owns(field):
    with pytest.raises(
        ValueError,
        match=(
            rf"custom_fields may not map {field!r} "
            r"\(excluded: \['creds', 'interfaces', 'ip'\]\)"
        ),
    ):
        _NetBoxInventory(url=DEAD, custom_fields={field: "cf"})


def test_extra_custom_fields_must_be_a_list_not_a_bare_string():
    # `list("owner")` is five phantom per-character custom fields.
    with pytest.raises(
        ValueError, match="extra_custom_fields must be a list of NetBox custom field names, got str"
    ):
        _NetBoxInventory(url=DEAD, extra_custom_fields="owner")
    with pytest.raises(
        ValueError,
        match="extra_custom_fields must be a list of NetBox custom field names, got list",
    ):
        _NetBoxInventory(url=DEAD, extra_custom_fields=[3])


def test_custom_fields_must_be_a_mapping():
    with pytest.raises(
        ValueError,
        match="custom_fields must be a table of record field -> NetBox custom field name, got list",
    ):
        _NetBoxInventory(url=DEAD, custom_fields=["sw_version"])


@pytest.mark.parametrize("reserved", ["id", "serial", "asset_tag", "status", "tags"])
def test_extra_custom_fields_may_not_shadow_what_the_backend_puts_in_extra(reserved):
    with pytest.raises(
        ValueError,
        match=(
            rf"extra_custom_fields names {reserved!r}, which this backend already puts in "
            r"record\.extra from the device itself"
        ),
    ):
        _NetBoxInventory(url=DEAD, extra_custom_fields=[reserved])


def test_a_trailing_slash_in_the_url_is_normalised_away():
    # The snapshot cache's slug hashes `self.url`, so two spellings of one
    # inventory must not be able to look like two.
    inv = _NetBoxInventory(url="https://netbox.example.com/")
    assert inv.url == "https://netbox.example.com"
    assert inv.label == "netbox:https://netbox.example.com"
    assert inv.label == _NetBoxInventory(url="https://netbox.example.com").label


def test_the_http_session_is_closed_after_the_fetch(monkeypatch):
    closed = []
    with NetBoxStub([device(1, "d1")]) as stub:
        import pynetbox

        real_api = pynetbox.api

        def spy(*a, **kw):
            api = real_api(*a, **kw)
            real_close = api.http_session.close

            def close():
                closed.append(True)
                real_close()

            api.http_session.close = close
            return api

        monkeypatch.setattr(pynetbox, "api", spy)
        assert _inv(stub).list_keys() == ["d1"]
    assert closed, "pynetbox builds its own requests.Session and never closes it"


def test_the_config_model_s_defaults_are_the_constructor_s():
    """One set of defaults: the model and ``_NetBoxInventory`` must never drift apart."""
    import inspect

    from otto.inventory.config import NetBoxInventoryConfig

    params = inspect.signature(_NetBoxInventory).parameters
    fields = NetBoxInventoryConfig.model_fields
    assert set(fields) <= set(params)
    for name, field in sorted(fields.items()):
        if field.is_required():
            assert params[name].default is inspect.Parameter.empty, name
        else:
            assert field.get_default(call_default_factory=True) == params[name].default, name


# -- virtual machines: opt-in ----------------------------------------------

_VMS_PATH = "/api/virtualization/virtual-machines/"


def _vm_requests(stub):
    return [r for r in stub.requests if r.startswith("/api/virtualization/")]


def test_a_device_only_configuration_makes_zero_virtual_machine_requests():
    # A token scoped to dcim only must keep working: the default never so much
    # as asks the virtualization app, even when the instance has VMs.
    with NetBoxStub([device(1, "d1")], virtual_machines=[virtual_machine(7, "vm1")]) as stub:
        assert _inv(stub).list_keys() == ["d1"]
        assert _vm_requests(stub) == []
        assert stub.vm_queries == []


def test_a_device_only_configuration_never_reaches_the_virtualization_app(monkeypatch):
    """Not one attribute read under ``api.virtualization`` — not merely no HTTP call."""
    import pynetbox

    touched = []
    real_api = pynetbox.api

    class Tripwire:
        def __getattr__(self, name):
            touched.append(name)
            raise AssertionError(f"api.virtualization.{name} read by a device-only config")

    def spy(*a, **kw):
        api = real_api(*a, **kw)
        api.virtualization = Tripwire()
        return api

    monkeypatch.setattr(pynetbox, "api", spy)
    with NetBoxStub([device(1, "d1")]) as stub:
        assert _inv(stub).list_keys() == ["d1"]
    assert touched == []


def test_virtual_machines_are_records_with_is_virtual_and_their_cluster():
    vms = [
        virtual_machine(7, "vm1", custom_fields={"owner": "ops"}),
        virtual_machine(8, "vm2", ip="10.1.0.2/24", site=None, cluster=None, platform=None),
        virtual_machine(9, "vm3", ip="10.1.0.3/24"),
    ]
    with NetBoxStub([device(1, "d1")], virtual_machines=vms, page_size=2) as stub:
        inv = _inv(stub, virtual_machines=True, extra_custom_fields=["owner"])
        assert inv.list_keys() == ["d1", "vm1", "vm2", "vm3"]  # three VMs over two pages
        assert [r.split("?")[0] for r in _vm_requests(stub)] == [_VMS_PATH] * 2
        r = inv.lookup("vm1")
        assert r.ip == "10.1.0.1"
        assert (r.site, r.os_name, r.is_virtual) == ("lab-a", "Ubuntu", True)
        assert (r.rack, r.shelf, r.board) == (None, None, None)
        assert not ({"rack", "shelf", "board"} & r.model_fields_set)
        assert r.extra == {
            "id": 7,
            "cluster": "vmc-1",
            "status": "active",
            "tags": ["lab"],
            "owner": "ops",
        }
        assert r.model_fields_set <= NATIVE_SUPPLIES | {"extra"}
        r2 = inv.lookup("vm2")
        assert (r2.site, r2.os_name) == (None, None)
        assert r2.extra["cluster"] is None
        # The device beside them is exactly what it is without VMs.
        assert inv.lookup("d1") == _inv(stub, extra_custom_fields=["owner"]).lookup("d1")
        assert inv.lookup("d1").is_virtual is False
        assert_inventory_conforms(inv, expected_keys=["d1", "vm1", "vm2", "vm3"])


@pytest.mark.parametrize("field", ["site", "platform", "cluster", "status", "tags"])
def test_a_virtual_machine_missing_a_field_is_named_as_one(field):
    # One policy for every kind, the device one: a field the list lacks is
    # read from the detail page, and a field that page lacks too is an error
    # naming the object.
    vm = virtual_machine(7, "vm1", drop=(field,))
    with NetBoxStub([device(1, "d1")], virtual_machines=[vm]) as stub:
        with pytest.raises(InventoryError) as excinfo:
            _inv(stub, virtual_machines=True).list_keys()
        assert str(excinfo.value) == (
            f"netbox inventory {stub.base}: virtual machine 'vm1' (id 7): "
            f'AttributeError: object has no attribute "{field}"'
        )
        assert _paths(stub) == ["/api/dcim/devices/", _VMS_PATH, f"{_VMS_PATH}7/"]


def test_vm_filter_is_forwarded_to_the_virtual_machine_endpoint_only():
    with NetBoxStub([device(1, "d1")], virtual_machines=[virtual_machine(7, "vm1")]) as stub:
        _inv(
            stub,
            filter={"site": "lab-a"},
            virtual_machines=True,
            vm_filter={"cluster": "vmc-1", "status": "active"},
        ).list_keys()
        assert stub.queries[0]["site"] == ["lab-a"]
        assert "cluster" not in stub.queries[0]
        assert stub.vm_queries[0]["cluster"] == ["vmc-1"]
        assert stub.vm_queries[0]["status"] == ["active"]
        assert "site" not in stub.vm_queries[0]


def test_vm_filter_without_virtual_machines_is_refused():
    with pytest.raises(ValueError, match=r"vm_filter has no effect unless virtual_machines = true"):
        _NetBoxInventory(url=DEAD, vm_filter={"cluster": "vmc-1"})


def test_vm_ip_source_cannot_be_oob_ip():
    with pytest.raises(
        ValueError,
        match=(
            r"vm_ip_source cannot be 'oob_ip': NetBox has no out-of-band address on a "
            r"virtual machine; use 'primary_ip4' or 'cf:<custom field>'"
        ),
    ):
        _NetBoxInventory(url=DEAD, virtual_machines=True, vm_ip_source="oob_ip")


@pytest.mark.parametrize("bad", ["bogus", "cf:"])
def test_vm_ip_source_must_name_a_source_a_virtual_machine_has(bad):
    with pytest.raises(
        ValueError,
        match=rf"vm_ip_source must be 'primary_ip4' or 'cf:<custom field>', got {bad!r}",
    ):
        _NetBoxInventory(url=DEAD, virtual_machines=True, vm_ip_source=bad)


def test_vm_ip_source_without_virtual_machines_is_refused():
    with pytest.raises(
        ValueError, match=r"vm_ip_source has no effect unless virtual_machines = true"
    ):
        _NetBoxInventory(url=DEAD, vm_ip_source="cf:mgmt")


def test_devices_over_oob_and_virtual_machines_over_primary_ip4_in_one_inventory():
    devices = [
        device(1, "d1", ip="10.0.0.1/24", oob_ip="10.9.9.1/32"),
        device(2, "d-noob", ip="10.0.0.2/24"),  # a primary address, but no oob_ip
    ]
    vms = [virtual_machine(7, "vm1", ip="10.1.0.1/24"), virtual_machine(8, "vm-noip", ip=None)]
    with NetBoxStub(devices, virtual_machines=vms) as stub:
        inv = _inv(stub, ip_source="oob_ip", virtual_machines=True)
        assert inv.vm_ip_source == "primary_ip4"
        assert inv.list_keys() == ["d1", "vm1"]
        assert inv.lookup("d1").ip == "10.9.9.1"
        assert inv.lookup("vm1").ip == "10.1.0.1"
        with pytest.raises(InventoryError) as excinfo:
            inv.lookup("d-noob")
        assert str(excinfo.value) == (
            f"netbox inventory {stub.base}: device 'd-noob' (id 2) has no address at "
            "ip_source 'oob_ip'"
        )
        with pytest.raises(InventoryError) as excinfo:
            inv.lookup("vm-noip")
        assert str(excinfo.value) == (
            f"netbox inventory {stub.base}: virtual machine 'vm-noip' (id 8) has no address at "
            "vm_ip_source 'primary_ip4'"
        )


def test_a_virtual_machine_address_from_a_custom_field():
    devices = [device(1, "d1", custom_fields={"mgmt": "10.8.8.1/24"})]
    vm = virtual_machine(7, "vm1", custom_fields={"mgmt": "10.8.8.9/24"})
    with NetBoxStub(devices, virtual_machines=[vm]) as stub:
        inv = _inv(stub, virtual_machines=True, vm_ip_source="cf:mgmt")
        assert inv.lookup("vm1").ip == "10.8.8.9"
        assert inv.lookup("d1").ip == "10.0.0.1"  # devices keep their own ip_source


def test_is_virtual_is_the_kind_s_for_a_virtual_machine_whatever_a_custom_field_says():
    # A device may be mapped virtual (an emulator modelled as a device), as
    # today; a virtual machine is virtual, and no custom field un-says it.
    devices = [
        device(1, "emu", custom_fields={"simulation": True}),
        device(2, "hw", custom_fields={"simulation": False}),
    ]
    vms = [
        virtual_machine(7, "vm-false", custom_fields={"simulation": False}),
        virtual_machine(8, "vm-true", custom_fields={"simulation": True}),
    ]
    with NetBoxStub(devices, virtual_machines=vms) as stub:
        inv = _inv(stub, virtual_machines=True, custom_fields={"is_virtual": "simulation"})
        assert inv.lookup("emu").is_virtual is True
        assert inv.lookup("hw").is_virtual is False
        assert inv.lookup("vm-false").is_virtual is True
        assert inv.lookup("vm-true").is_virtual is True


def test_a_device_and_a_virtual_machine_with_one_name_fail_naming_both_kinds():
    devices = [device(3, "edge-17")]
    with NetBoxStub(devices, virtual_machines=[virtual_machine(7, "edge-17")]) as stub:
        with pytest.raises(InventoryError) as excinfo:
            _inv(stub, virtual_machines=True).list_keys()
        assert str(excinfo.value) == (
            f"netbox inventory {stub.base}: duplicate name 'edge-17' "
            "(device id 3 and virtual machine id 7)"
        )


@pytest.mark.parametrize(
    ("device_ip", "vm_ip"), [(None, "10.1.0.1/24"), ("10.0.0.1/24", None), (None, None)]
)
def test_the_duplicate_check_covers_addressless_entries_of_either_kind(device_ip, vm_ip):
    devices = [device(3, "edge-17", ip=device_ip)]
    vms = [virtual_machine(7, "edge-17", ip=vm_ip)]
    with (
        NetBoxStub(devices, virtual_machines=vms) as stub,
        pytest.raises(
            InventoryError,
            match=r"duplicate name 'edge-17' \(device id 3 and virtual machine id 7\)",
        ),
    ):
        _inv(stub, virtual_machines=True).list_keys()


def test_two_virtual_machines_with_one_name_fail_naming_both_ids():
    # NetBox scopes a VM's name to its cluster, so two clusters can each hold one.
    vms = [virtual_machine(7, "dup"), virtual_machine(8, "dup", cluster="vmc-2")]
    with NetBoxStub([], virtual_machines=vms) as stub:
        with pytest.raises(InventoryError) as excinfo:
            _inv(stub, virtual_machines=True).list_keys()
        assert str(excinfo.value) == (
            f"netbox inventory {stub.base}: duplicate virtual machine name 'dup' "
            "(virtual machine ids 7 and 8)"
        )


def test_unnamed_and_addressless_virtual_machines_are_skipped_and_named_by_kind():
    devices = [device(1, None), device(2, "d-noip", ip=None), device(3, "d1")]
    vms = [
        virtual_machine(7, None),
        virtual_machine(8, ""),
        virtual_machine(9, "vm-noip", ip=None),
        virtual_machine(10, "vm1"),
    ]
    with NetBoxStub(devices, virtual_machines=vms) as stub:
        inv = _inv(stub, virtual_machines=True)
        assert inv.list_keys() == ["d1", "vm1"]
        assert inv.unnamed_device_ids == [1]
        assert inv.unnamed_virtual_machine_ids == [7, 8]
        assert inv.addressless_device_names == ["d-noip"]
        assert inv.addressless_virtual_machine_names == ["vm-noip"]
        with pytest.raises(
            InventoryError,
            match=(
                r": virtual machine 'vm-noip' \(id 9\) has no address at "
                r"vm_ip_source 'primary_ip4'"
            ),
        ):
            inv.lookup("vm-noip")
        with pytest.raises(
            InventoryError, match=r": device 'd-noip' \(id 2\) has no address at ip_source"
        ):
            inv.lookup("d-noip")


def test_the_virtual_machine_skip_lists_are_empty_before_the_first_fetch():
    inv = _NetBoxInventory(url=DEAD, virtual_machines=True)
    assert inv.unnamed_virtual_machine_ids == []
    assert inv.addressless_virtual_machine_names == []


def test_a_virtual_machine_that_fails_validation_is_named_as_one():
    vm = virtual_machine(7, "vm1", custom_fields={"mgmt": "not an address"})
    with (
        NetBoxStub([], virtual_machines=[vm]) as stub,
        pytest.raises(
            InventoryError,
            match=(
                r"virtual machine 'vm1' \(id 7\): ip: .* "
                r"\(ip read from vm_ip_source 'cf:mgmt'\)"
            ),
        ),
    ):
        _inv(stub, virtual_machines=True, vm_ip_source="cf:mgmt").list_keys()


def test_a_virtual_machine_missing_a_required_field_is_named_as_one():
    vm = virtual_machine(7, "vm1", drop=("custom_fields",))
    with (
        NetBoxStub([], virtual_machines=[vm]) as stub,
        pytest.raises(
            InventoryError,
            match=rf"netbox inventory {stub.base}: virtual machine 'vm1' \(id 7\): AttributeError",
        ),
    ):
        _inv(stub, virtual_machines=True).list_keys()


def test_the_virtual_machine_fetch_shares_the_device_fetch_s_error_wrapping():
    # A token scoped to dcim: devices answer, the virtualization app refuses.
    with NetBoxStub([device(1, "d1")], forbidden={_VMS_PATH}) as stub:
        inv = _inv(stub, virtual_machines=True)
        with pytest.raises(InventoryError) as excinfo:
            inv.list_keys()
        assert str(excinfo.value).startswith(
            f"netbox inventory {stub.base}: listing virtual machines: RequestError: "
        )
        assert [r.split("?")[0] for r in stub.requests] == ["/api/dcim/devices/", _VMS_PATH]


def test_with_virtual_machines_cluster_is_reserved_in_extra():
    _NetBoxInventory(url=DEAD, extra_custom_fields=["cluster"])  # devices only: free to use
    with pytest.raises(
        ValueError,
        match=(
            r"extra_custom_fields names 'cluster', which this backend already puts in "
            r"record\.extra from the device or virtual machine itself"
        ),
    ):
        _NetBoxInventory(url=DEAD, virtual_machines=True, extra_custom_fields=["cluster"])


def test_the_reserved_extra_keys_are_exactly_what_each_kind_writes():
    """A key the mapping writes must be reserved, and a reserved key must be written."""
    with NetBoxStub([device(1, "d1")], virtual_machines=[virtual_machine(7, "vm1")]) as stub:
        devices_only = _inv(stub)
        both = _inv(stub, virtual_machines=True)
        assert set(devices_only.lookup("d1").extra) == devices_only.reserved_extra_keys
        assert set(both.lookup("d1").extra) | set(both.lookup("vm1").extra) == (
            both.reserved_extra_keys
        )
        assert devices_only.reserved_extra_keys == {"id", "serial", "asset_tag", "status", "tags"}
        assert both.reserved_extra_keys == devices_only.reserved_extra_keys | {"cluster"}


def test_an_existing_configuration_keeps_its_snapshot_identity(tmp_path):
    """The new keys at their defaults change neither ``same_as`` nor the snapshot slug.

    The literals are this table's normalized configuration and slug from
    before virtual machines existed: a snapshot an older otto wrote must stay
    this one's snapshot.
    """
    from datetime import timedelta

    from otto.inventory.cache import snapshot_slug_material

    table = {"url": "https://netbox.example.com/", "filter": {"site": "lab-a"}, "cache_ttl": "1h"}
    old = _compiled(tmp_path, table)
    assert old.prepared.normalized.thaw_json() == {
        "url": "https://netbox.example.com/",
        "token_env": "NETBOX_TOKEN",
        "filter": {"site": "lab-a"},
        "ip_source": "primary_ip4",
        "custom_fields": None,
        "extra_custom_fields": None,
        "timeout": 30.0,
    }
    slug = snapshot_slug_material(
        old.prepared.backend,
        "netbox:https://netbox.example.com",
        old.prepared.normalized,
        cache_ttl=timedelta(hours=1),
    )
    assert slug == (
        "netbox|netbox:https://netbox.example.com|"
        '{"custom_fields": null, "extra_custom_fields": null, "filter": {"site": "lab-a"}, '
        '"ip_source": "primary_ip4", "timeout": 30.0, "token_env": "NETBOX_TOKEN"}|ttl=3600'
    )
    # Spelling the defaults out is the same inventory; turning VMs on is not.
    spelled = {"virtual_machines": False, "vm_filter": None, "vm_ip_source": "primary_ip4"}
    assert old.same_as(other=_compiled(tmp_path, {**table, **spelled}))
    with_vms = _compiled(tmp_path, {**table, "virtual_machines": True})
    assert not old.same_as(other=with_vms)
    assert dict(with_vms.prepared.normalized)["virtual_machines"] is True
    assert "vm_ip_source" not in dict(with_vms.prepared.normalized)
    other_source = _compiled(tmp_path, {**table, "virtual_machines": True, "vm_ip_source": "cf:m"})
    assert not with_vms.same_as(other=other_source)


def test_the_settings_table_turns_virtual_machines_on(tmp_path):
    with NetBoxStub([device(1, "d1")], virtual_machines=[virtual_machine(7, "vm1")]) as stub:
        inv = construct_inventory(
            compiled=_compiled(
                tmp_path,
                {"url": stub.base, "virtual_machines": True, "vm_filter": {"cluster": "vmc-1"}},
            )
        )
        assert inv.list_keys() == ["d1", "vm1"]
        assert stub.vm_queries[0]["cluster"] == ["vmc-1"]
