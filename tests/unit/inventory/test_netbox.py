"""The netbox backend against a local stub — the real pynetbox client, no network (spec §9.2, §14).

Every match= below is anchored on the phrase the guard exists for, never on a
bare word a repr or a locals dump could satisfy.
"""

import time
from datetime import timedelta
from pathlib import Path

import pytest

from otto.inventory import InventoryError, InventoryKeyError, get_inventory_backend_class
from otto.inventory.config import CompiledInventory, construct_inventory
from otto.inventory.netbox import NATIVE_SUPPLIES, NetBoxInventory
from otto.testing import assert_inventory_conforms

from .netbox_stub import TOKEN, NetBoxStub, device, self_signed_cert

# A port nothing listens on: `otto` never opens one, and 9 is the stdlib
# discard service, so a connect() here is refused rather than answered.
DEAD = "http://127.0.0.1:9"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("NETBOX_TOKEN", TOKEN)


def _inv(stub, **kw):
    return NetBoxInventory(url=stub.base, **kw)


def test_netbox_is_registered_and_construction_is_lazy():
    assert get_inventory_backend_class("netbox") is NetBoxInventory
    inv = NetBoxInventory(url=DEAD)  # nothing listens; no error until first use
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
    inv = NetBoxInventory(url=DEAD)  # construction is silent even with no token anywhere
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
        NetBoxInventory(url=DEAD, custom_fields={"bogus": "cf"})


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


def test_ip_source_must_name_a_source_the_backend_can_read():
    with pytest.raises(
        ValueError,
        match=r"ip_source must be 'primary_ip4', 'oob_ip', or 'cf:<custom field>', got 'bogus'",
    ):
        NetBoxInventory(url=DEAD, ip_source="bogus")
    with pytest.raises(ValueError, match="got 'cf:'"):
        NetBoxInventory(url=DEAD, ip_source="cf:")  # a prefix with no field name


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
        NetBoxInventory(url="")


def test_a_netbox_that_stops_answering_times_out_instead_of_blocking_the_command():
    """An UNREACHABLE NetBox must not hold a lab-bound command for the kernel's TCP timeout.

    pynetbox issues ``http_session.get(...)`` with no ``timeout``, and
    requests' ``Session.send`` then passes ``timeout=None`` EXPLICITLY — so
    the bound has to come from an adapter that OVERWRITES a ``None`` that is
    already there. A ``setdefault`` would never fire, and the elapsed
    assertion below is what that distinction goes red on.
    """
    with NetBoxStub([device(1, "d1")], delay=1.0) as stub:
        inv = _inv(stub, timeout=0.2)
        started = time.monotonic()
        with pytest.raises(InventoryError, match=r"netbox inventory .*: Read timed out"):
            inv.list_keys()
        elapsed = time.monotonic() - started
    assert elapsed < 0.9, f"the request was never bounded: it took {elapsed:.2f}s"


def test_the_default_timeout_is_thirty_seconds():
    """Generous, because a filtered fetch over a large instance is a real query."""
    assert NetBoxInventory(url=DEAD).timeout == 30.0


@pytest.mark.parametrize("bad", [0, -1, "30", None, True])
def test_a_timeout_that_is_not_a_positive_number_is_refused_at_construction(bad):
    """``True`` is in here on purpose: ``bool`` is an ``int``, and ``timeout = true``
    in a TOML table would otherwise quietly mean "one second"."""
    with pytest.raises(ValueError, match="timeout must be a positive number of seconds"):
        NetBoxInventory(url=DEAD, timeout=bad)


def test_a_refused_timeout_names_the_settings_file_and_the_backend(tmp_path):
    """The construction path turns the ValueError into an InventoryError the user can act on."""
    compiled = CompiledInventory(
        backend="netbox",
        kwargs={"url": DEAD, "timeout": 0},
        creds_file=None,
        cache_ttl=timedelta(0),
        anchor_dir=tmp_path,
        origin=str(tmp_path / ".otto" / "settings.toml"),
    )
    with pytest.raises(
        InventoryError,
        match=r"settings\.toml: \[inventory\] backend 'netbox': "
        r"timeout must be a positive number of seconds",
    ):
        construct_inventory(compiled)


def test_repo_dir_is_accepted_and_reaches_no_instance_attribute(tmp_path):
    # The registry's uniform constructor contract: construct_inventory always
    # passes repo_dir. Nothing here interprets a path, so two repos declaring
    # the same table build an inventory identical in EVERY attribute — the
    # whole `vars()`, not a chosen pair, because `CompiledInventory.same_as`
    # ignores anchor_dir and an attribute that quietly remembered it would
    # make two such repos disagree about an inventory they call the same.
    a = NetBoxInventory(tmp_path / "repo-a", url=DEAD)
    b = NetBoxInventory(tmp_path / "repo-b", url=DEAD)
    assert vars(a) == vars(b)
    assert not [v for v in vars(a).values() if isinstance(v, Path)]


def test_a_bad_kwarg_names_the_settings_file_and_the_backend(tmp_path):
    # Why the constructor raises ValueError rather than InventoryError:
    # construct_inventory wraps it with the origin the user must go and edit.
    compiled = CompiledInventory(
        backend="netbox",
        kwargs={"url": DEAD, "ip_source": "bogus"},
        creds_file=None,
        cache_ttl=timedelta(0),
        anchor_dir=tmp_path,
        origin=str(tmp_path / ".otto" / "settings.toml"),
    )
    with pytest.raises(InventoryError, match=r"settings\.toml: \[inventory\] backend 'netbox':"):
        construct_inventory(compiled)


def test_an_unknown_kwarg_names_the_settings_file_and_the_backend(tmp_path):
    compiled = CompiledInventory(
        backend="netbox",
        kwargs={"url": DEAD, "urll": "typo"},
        creds_file=None,
        cache_ttl=timedelta(0),
        anchor_dir=tmp_path,
        origin=str(tmp_path / ".otto" / "settings.toml"),
    )
    with pytest.raises(InventoryError, match=r"backend 'netbox':.*unexpected keyword argument"):
        construct_inventory(compiled)


# -- round 1: the wire-shape seam ------------------------------------------


def test_a_relative_ca_bundle_path_is_refused_at_construction():
    # An [inventory] table is COMMITTED; a relative path in one resolves
    # against whatever directory otto was run from, not the repo.
    with pytest.raises(
        ValueError,
        match=(
            r"verify must be an absolute path to a CA bundle \(or true/false\); "
            r"got 'certs/ca\.pem' — a relative path resolves against the process working "
            r"directory, not the repo"
        ),
    ):
        NetBoxInventory(url=DEAD, verify="certs/ca.pem")


def test_an_absolute_ca_bundle_path_is_kept_and_a_tilde_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert NetBoxInventory(url=DEAD, verify=str(tmp_path / "ca.pem")).verify == str(
        tmp_path / "ca.pem"
    )
    assert NetBoxInventory(url=DEAD, verify="~/ca.pem").verify == str(tmp_path / "ca.pem")
    assert NetBoxInventory(url=DEAD, verify=False).verify is False


def test_verify_true_refuses_a_certificate_no_trust_store_knows(tmp_path):
    # The stub speaks real TLS with a self-signed certificate, so `verify` has
    # something to actually verify — over plain HTTP the setting is inert and
    # deleting it entirely would leave every other test green.
    # Not combinable: the inner match= reads `stub.base`, which only the outer
    # `with` binds.
    with NetBoxStub([device(1, "d1")], tls=self_signed_cert(tmp_path)) as stub:  # noqa: SIM117
        with pytest.raises(InventoryError, match=f"netbox inventory {stub.base}"):
            _inv(stub).list_keys()


@pytest.mark.filterwarnings("ignore::urllib3.exceptions.InsecureRequestWarning")
def test_verify_false_accepts_it(tmp_path):
    with NetBoxStub([device(1, "d1")], tls=self_signed_cert(tmp_path)) as stub:
        assert _inv(stub, verify=False).list_keys() == ["d1"]


def test_verify_with_the_certificate_as_the_ca_bundle_accepts_it(tmp_path):
    certfile, keyfile = self_signed_cert(tmp_path)
    with NetBoxStub([device(1, "d1")], tls=(certfile, keyfile)) as stub:
        assert _inv(stub, verify=str(certfile)).list_keys() == ["d1"]


def test_a_device_missing_a_field_names_the_device_and_the_url():
    # An older or plugin-trimmed serializer omits `position`; that must not
    # escape as a bare AttributeError naming neither the device nor the URL.
    # Not combinable: the inner match= reads `stub.base`.
    with NetBoxStub([device(1, "d1", drop=("position",))]) as stub:  # noqa: SIM117
        with pytest.raises(
            InventoryError,
            match=rf"netbox inventory {stub.base}: device 'd1' \(id 1\): AttributeError",
        ):
            _inv(stub).list_keys()


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
    assert NetBoxInventory(url=DEAD).unnamed_device_ids == []


@pytest.mark.parametrize("field", ["ip", "creds", "interfaces"])
def test_custom_fields_may_not_map_a_field_the_backend_owns(field):
    with pytest.raises(
        ValueError,
        match=(
            rf"custom_fields may not map {field!r} "
            r"\(excluded: \['creds', 'interfaces', 'ip'\]\)"
        ),
    ):
        NetBoxInventory(url=DEAD, custom_fields={field: "cf"})


def test_extra_custom_fields_must_be_a_list_not_a_bare_string():
    # `list("owner")` is five phantom per-character custom fields.
    with pytest.raises(
        ValueError, match="extra_custom_fields must be a list of NetBox custom field names, got str"
    ):
        NetBoxInventory(url=DEAD, extra_custom_fields="owner")
    with pytest.raises(
        ValueError,
        match="extra_custom_fields must be a list of NetBox custom field names, got list",
    ):
        NetBoxInventory(url=DEAD, extra_custom_fields=[3])


def test_custom_fields_must_be_a_mapping():
    with pytest.raises(
        ValueError,
        match="custom_fields must be a table of record field -> NetBox custom field name, got list",
    ):
        NetBoxInventory(url=DEAD, custom_fields=["sw_version"])


@pytest.mark.parametrize("reserved", ["id", "serial", "asset_tag", "status", "tags"])
def test_extra_custom_fields_may_not_shadow_what_the_backend_puts_in_extra(reserved):
    with pytest.raises(
        ValueError,
        match=(
            rf"extra_custom_fields names {reserved!r}, which this backend already puts in "
            r"record\.extra from the device itself"
        ),
    ):
        NetBoxInventory(url=DEAD, extra_custom_fields=[reserved])


def test_a_trailing_slash_in_the_url_is_normalised_away():
    # The snapshot cache's slug hashes `self.url`, so two spellings of one
    # inventory must not be able to look like two.
    inv = NetBoxInventory(url="https://netbox.example.com/")
    assert inv.url == "https://netbox.example.com"
    assert inv.label == "netbox:https://netbox.example.com"
    assert inv.label == NetBoxInventory(url="https://netbox.example.com").label


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
