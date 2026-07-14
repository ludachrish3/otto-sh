"""Unit tests for the JSON Schema generation module."""

import pytest

import otto.host.os_profile as op
from otto.models.host import HostSpec
from otto.models.jsonschema import build_schemas


def test_default_set_of_documents():
    docs = build_schemas()
    assert set(docs) >= {
        "unix-host",
        "embedded-host",
        "lab",
        "link",
        "settings",
        "reservations",
        "monitor-meta",
        "monitor-export",
    }


def test_each_doc_is_a_self_describing_json_schema():
    for stem, doc in build_schemas().items():
        assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert "$id" in doc
        assert stem in doc["$id"]
        assert "title" in doc


def test_friendly_title_wins_over_the_models_class_name_title():
    # model_json_schema() emits its own title (the class name); the decoration
    # must override it with the friendly one, not be clobbered by it.
    docs = build_schemas()
    assert docs["settings"]["title"] == "otto settings.toml"
    assert docs["unix-host"]["title"] == "otto unix-host"
    assert docs["reservations"]["title"] == "otto reservations"
    assert docs["lab"]["title"] == "otto lab.json"
    assert docs["link"]["title"] == "otto link"


def test_host_specs_forbid_unknown_keys():
    docs = build_schemas()
    assert docs["unix-host"]["additionalProperties"] is False
    assert docs["embedded-host"]["additionalProperties"] is False


def test_lab_schema_emitted():
    docs = build_schemas(builtins_only=True)
    assert "hosts" not in docs  # hard cutover: array-only schema retired
    lab = docs["lab"]
    assert lab["type"] == "object"
    assert set(lab["properties"]) == {"hosts", "links"}
    assert lab["properties"]["hosts"]["type"] == "array"
    assert lab["properties"]["links"]["type"] == "array"
    assert lab["additionalProperties"] is False
    assert "^_" in lab.get("patternProperties", {})  # top-level comment keys


def test_link_schema_emitted():
    docs = build_schemas(builtins_only=True)
    link = docs["link"]
    assert link["title"] == "otto link"
    assert "endpoints" in link["properties"]


def test_lab_hosts_property_is_an_anyof_array_with_discriminator():
    lab = build_schemas()["lab"]
    hosts = lab["properties"]["hosts"]
    assert hosts["type"] == "array"
    items = hosts["items"]
    # anyOf, not oneOf — minimal hosts validate against >1 spec.
    assert "anyOf" in items
    assert "oneOf" not in items
    assert {ref["$ref"] for ref in items["anyOf"]} == {
        "#/$defs/UnixHostSpec",
        "#/$defs/EmbeddedHostSpec",
    }
    disc = items["discriminator"]
    assert disc["propertyName"] == "os_type"
    # Every registered os_type name is mapped to its spec's $def.
    assert disc["mapping"] == {
        "unix": "#/$defs/UnixHostSpec",
        "embedded": "#/$defs/EmbeddedHostSpec",
        "zephyr": "#/$defs/EmbeddedHostSpec",
    }
    assert "UnixHostSpec" in lab["$defs"]
    assert "EmbeddedHostSpec" in lab["$defs"]


def test_custom_registered_spec_appears(monkeypatch):
    # A custom host class + spec registered at runtime must flow into both its
    # own file and the hosts wrapper, without touching the real registry.
    class AcmeSpec(HostSpec):
        pass

    monkeypatch.setitem(op._HOST_SPECS, "acme", AcmeSpec)
    docs = build_schemas()
    mapping = docs["lab"]["properties"]["hosts"]["items"]["discriminator"]["mapping"]
    assert "acme" in mapping
    assert mapping["acme"] == "#/$defs/AcmeSpec"
    assert "acme" in docs  # its own per-spec file (stem from the class name)


def test_stem_handles_runs_of_capitals(monkeypatch):
    # A contrib spec name with consecutive capitals still kebab-cases cleanly.
    class ACMEHostSpec(HostSpec):
        pass

    monkeypatch.setitem(op._HOST_SPECS, "acme", ACMEHostSpec)
    docs = build_schemas()
    assert "acme-host" in docs  # not 'a-c-m-e-host'


def test_builtins_only_excludes_custom_specs(monkeypatch):
    # build_schemas(builtins_only=True) emits only the in-tree host types, even
    # when a custom spec is registered.
    class AcmeSpec(HostSpec):
        pass

    monkeypatch.setitem(op._HOST_SPECS, "acme", AcmeSpec)

    full = build_schemas()
    assert "acme" in full
    assert "acme" in full["lab"]["properties"]["hosts"]["items"]["discriminator"]["mapping"]

    builtins = build_schemas(builtins_only=True)
    assert "acme" not in builtins
    mapping = builtins["lab"]["properties"]["hosts"]["items"]["discriminator"]["mapping"]
    assert "acme" not in mapping
    assert set(mapping) == {
        "unix",
        "embedded",
        "zephyr",
    }


class TestSelectorEnums:
    def test_unix_host_schema_has_registry_enums(self):
        from otto.models.jsonschema import build_schemas

        props = build_schemas()["unix-host"]["properties"]
        # Menu fields accept scalar-or-list; the registry enum rides both branches.
        vt = props["valid_terms"]
        assert vt["anyOf"][0]["enum"] == ["ssh", "telnet"]  # scalar
        assert vt["anyOf"][1]["items"]["enum"] == ["ssh", "telnet"]  # array
        vx = props["valid_transfers"]
        assert vx["anyOf"][0]["enum"] == ["ftp", "nc", "scp", "sftp"]
        assert vx["anyOf"][1]["items"]["enum"] == ["ftp", "nc", "scp", "sftp"]
        # Scalar pins are present but have no injected enum (nullable optional).
        assert "term" in props
        assert "enum" not in props["term"]

    def test_embedded_host_schema_has_registry_enums(self):
        from otto.models.jsonschema import build_schemas

        props = build_schemas()["embedded-host"]["properties"]
        vx = props["valid_transfers"]
        assert vx["anyOf"][0]["enum"] == ["console", "tftp"]
        assert vx["anyOf"][1]["items"]["enum"] == ["console", "tftp"]
        vt = props["valid_terms"]
        assert vt["anyOf"][0]["enum"] == ["telnet"]
        assert vt["anyOf"][1]["items"]["enum"] == ["telnet"]
        assert "term" in props
        assert "enum" not in props["term"]

    def test_hosts_array_defs_carry_enums(self):
        from otto.models.jsonschema import build_schemas

        defs = build_schemas()["lab"]["$defs"]
        unix_def = next(
            d
            for d in defs.values()
            if isinstance(d, dict)
            and d.get("properties", {}).get("os_type", {}).get("default") == "unix"
        )
        assert unix_def["properties"]["valid_transfers"]["anyOf"][1]["items"]["enum"] == [
            "ftp",
            "nc",
            "scp",
            "sftp",
        ]

    def test_custom_unix_transfer_appears_in_enum(self):
        from otto.host import transfer as xfer_mod
        from otto.host.transfer import UnixFileTransfer
        from otto.models.jsonschema import build_schemas

        class XmodemTransfer(UnixFileTransfer):
            host_families = frozenset({"unix"})

        xfer_mod.TRANSFER_BACKENDS.register("xmodem", XmodemTransfer)
        try:
            props = build_schemas()["unix-host"]["properties"]
            assert "xmodem" in props["valid_transfers"]["anyOf"][1]["items"]["enum"]
        finally:
            xfer_mod.TRANSFER_BACKENDS.unregister("xmodem")

    def test_menu_property_accepts_scalar_and_list(self):
        import jsonschema

        from otto.models.jsonschema import build_schemas

        vt_schema = build_schemas()["unix-host"]["properties"]["valid_transfers"]
        jsonschema.validate("scp", vt_schema)  # scalar OK
        jsonschema.validate(["scp", "sftp"], vt_schema)  # list OK
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate("bogus", vt_schema)  # out-of-enum scalar
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(["bogus"], vt_schema)  # out-of-enum in list


def test_monitor_export_schema_shape():
    docs = build_schemas(builtins_only=True)
    doc = docs["monitor-export"]
    assert doc["title"] == "Monitor historical export document"
    assert set(doc["required"]) == {"format", "sessions"}
    assert doc["properties"]["format"]["const"] == 1


def test_monitor_export_schema_carries_an_unreachable_fragment_def():
    """MonitorSessionFragment rides in ``$defs`` so export.gen.ts gets its TS type
    (via ``json-schema-to-typescript --unreachableDefinitions``), but it must stay
    unreachable from the document's own ``properties``/``required`` — the fragment
    is not part of the on-disk export format (see ``_monitor_export_schema``)."""
    doc = build_schemas(builtins_only=True)["monitor-export"]
    frag_def = doc["$defs"]["MonitorSessionFragment"]
    assert set(frag_def["required"]) == {"session"}
    assert set(frag_def["properties"]) == {
        "format",
        "session",
        "metrics",
        "events",
        "log_events",
        "deleted_event_ids",
        "chart_map",
        "meta",
    }
    # Not reachable from the export document's own root shape.
    assert set(doc["required"]) == {"format", "sessions"}
    assert "session" not in doc["properties"]
    # Reuses the SAME $defs the export document already carries for
    # SessionRecord's fields — no duplicate MetricRecord/EventRecord/etc.
    assert frag_def["properties"]["metrics"]["items"]["$ref"] == "#/$defs/MetricRecord"
    assert frag_def["properties"]["events"]["items"]["$ref"] == "#/$defs/EventRecord"


def test_monitor_export_schema_chart_map_is_deduped_to_one_shared_def():
    """SessionRecord and MonitorSessionFragment both declare a plain
    ``chart_map: dict[str, str]`` field. Pydantic inlines a plain-dict field's
    schema at each occurrence (it only hoists NAMED nested models to $defs),
    so without _dedupe_chart_map the two occurrences are structurally
    identical but textually unlinked — json-schema-to-typescript then
    synthesizes two names for them (``ChartMap``/``ChartMap1``, Plan 5b
    follow-ups #9). Both fields must instead $ref the SAME $defs/ChartMap
    entry, so the generated TS carries exactly one interface, reused by both.
    """
    doc = build_schemas(builtins_only=True)["monitor-export"]
    defs = doc["$defs"]
    assert "ChartMap" in defs
    assert defs["ChartMap"]["type"] == "object"
    assert defs["ChartMap"]["additionalProperties"] == {"type": "string"}
    assert defs["SessionRecord"]["properties"]["chart_map"] == {"$ref": "#/$defs/ChartMap"}
    assert defs["MonitorSessionFragment"]["properties"]["chart_map"] == {"$ref": "#/$defs/ChartMap"}
