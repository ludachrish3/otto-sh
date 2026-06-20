"""Unit tests for the JSON Schema generation module."""

import otto.host.os_profile as op
from otto.models import EmbeddedHostSpec, UnixHostSpec
from otto.models.host import HostSpec
from otto.models.jsonschema import build_schemas


def test_default_set_of_documents():
    docs = build_schemas()
    assert set(docs) >= {
        'unix-host', 'embedded-host', 'hosts', 'settings', 'reservations'
    }


def test_each_doc_is_a_self_describing_json_schema():
    for stem, doc in build_schemas().items():
        assert doc['$schema'] == 'https://json-schema.org/draft/2020-12/schema'
        assert '$id' in doc and stem in doc['$id']
        assert 'title' in doc


def test_friendly_title_wins_over_the_models_class_name_title():
    # model_json_schema() emits its own title (the class name); the decoration
    # must override it with the friendly one, not be clobbered by it.
    docs = build_schemas()
    assert docs['settings']['title'] == 'otto settings.toml'
    assert docs['unix-host']['title'] == 'otto unix-host'
    assert docs['reservations']['title'] == 'otto reservations'
    assert docs['hosts']['title'] == 'otto hosts.json'


def test_host_specs_forbid_unknown_keys():
    docs = build_schemas()
    assert docs['unix-host']['additionalProperties'] is False
    assert docs['embedded-host']['additionalProperties'] is False


def test_hosts_wrapper_is_an_anyof_array_with_discriminator():
    hosts = build_schemas()['hosts']
    assert hosts['type'] == 'array'
    items = hosts['items']
    # anyOf, not oneOf — minimal hosts validate against >1 spec.
    assert 'anyOf' in items and 'oneOf' not in items
    assert {ref['$ref'] for ref in items['anyOf']} == {
        '#/$defs/UnixHostSpec', '#/$defs/EmbeddedHostSpec'
    }
    disc = items['discriminator']
    assert disc['propertyName'] == 'os_type'
    # Every registered os_type name is mapped to its spec's $def.
    assert disc['mapping'] == {
        'unix': '#/$defs/UnixHostSpec',
        'embedded': '#/$defs/EmbeddedHostSpec',
        'zephyr': '#/$defs/EmbeddedHostSpec',
    }
    assert 'UnixHostSpec' in hosts['$defs'] and 'EmbeddedHostSpec' in hosts['$defs']


def test_custom_registered_spec_appears(monkeypatch):
    # A custom host class + spec registered at runtime must flow into both its
    # own file and the hosts wrapper, without touching the real registry.
    class AcmeSpec(HostSpec):
        pass

    monkeypatch.setitem(op._HOST_SPECS, 'acme', AcmeSpec)
    docs = build_schemas()
    assert 'acme' in docs['hosts']['items']['discriminator']['mapping']
    assert docs['hosts']['items']['discriminator']['mapping']['acme'] == '#/$defs/AcmeSpec'
    assert 'acme' in docs  # its own per-spec file (stem from the class name)


def test_stem_handles_runs_of_capitals(monkeypatch):
    # A contrib spec name with consecutive capitals still kebab-cases cleanly.
    class ACMEHostSpec(HostSpec):
        pass

    monkeypatch.setitem(op._HOST_SPECS, 'acme', ACMEHostSpec)
    docs = build_schemas()
    assert 'acme-host' in docs  # not 'a-c-m-e-host'


def test_builtins_only_excludes_custom_specs(monkeypatch):
    # build_schemas(builtins_only=True) emits only the in-tree host types, even
    # when a custom spec is registered.
    class AcmeSpec(HostSpec):
        pass

    monkeypatch.setitem(op._HOST_SPECS, 'acme', AcmeSpec)

    full = build_schemas()
    assert 'acme' in full and 'acme' in full['hosts']['items']['discriminator']['mapping']

    builtins = build_schemas(builtins_only=True)
    assert 'acme' not in builtins
    assert 'acme' not in builtins['hosts']['items']['discriminator']['mapping']
    assert set(builtins['hosts']['items']['discriminator']['mapping']) == {
        'unix', 'embedded', 'zephyr'
    }


class TestSelectorEnums:
    def test_unix_host_schema_has_registry_enums(self):
        from otto.models.jsonschema import build_schemas

        props = build_schemas()["unix-host"]["properties"]
        # Enum lives on the menu-array items, not the nullable pin scalars.
        assert props["valid_terms"]["items"]["enum"] == ["ssh", "telnet"]
        assert props["valid_transfers"]["items"]["enum"] == ["ftp", "nc", "scp", "sftp"]
        # Scalar pins are present but have no injected enum (nullable optional).
        assert "term" in props
        assert "enum" not in props["term"]

    def test_embedded_host_schema_has_registry_enums(self):
        from otto.models.jsonschema import build_schemas

        props = build_schemas()["embedded-host"]["properties"]
        # Enum lives on the menu-array items; both axes are family-filtered to
        # embedded-applicable backends — transfers to console/tftp, terms to
        # telnet only (ssh serves unix).
        assert props["valid_transfers"]["items"]["enum"] == ["console", "tftp"]
        assert props["valid_terms"]["items"]["enum"] == ["telnet"]
        # Scalar pin is present but has no injected enum.
        assert "term" in props
        assert "enum" not in props["term"]

    def test_hosts_array_defs_carry_enums(self):
        from otto.models.jsonschema import build_schemas

        defs = build_schemas()["hosts"]["$defs"]
        unix_def = next(
            d for d in defs.values()
            if isinstance(d, dict)
            and d.get("properties", {}).get("os_type", {}).get("default") == "unix"
        )
        assert unix_def["properties"]["valid_transfers"]["items"]["enum"] == [
            "ftp", "nc", "scp", "sftp"
        ]

    def test_custom_unix_transfer_appears_in_enum(self):
        from otto.host import transfer as xfer_mod
        from otto.host.transfer import UnixFileTransfer
        from otto.models.jsonschema import build_schemas

        class XmodemTransfer(UnixFileTransfer):
            host_families = frozenset({"unix"})

        saved = dict(xfer_mod._TRANSFER_BACKENDS)
        xfer_mod._TRANSFER_BACKENDS["xmodem"] = XmodemTransfer
        try:
            props = build_schemas()["unix-host"]["properties"]
            assert "xmodem" in props["valid_transfers"]["items"]["enum"]
        finally:
            xfer_mod._TRANSFER_BACKENDS.clear()
            xfer_mod._TRANSFER_BACKENDS.update(saved)
