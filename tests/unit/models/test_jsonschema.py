"""Unit tests for the JSON Schema generation module."""

import pytest

import otto.host.os_profile as op
from otto.models.host import HostSpec
from otto.models.jsonschema import build_schemas


def _element_host_discriminator_mapping(docs):
    """The ``os_type`` discriminator mapping of an element's host entries.

    In v2 the host ``anyOf`` lives under the ``elements`` wrapper, not at the
    document root — one helper so the three tests that read it share one path.
    """
    return docs["lab"]["$defs"]["ElementSpec"]["properties"]["hosts"]["items"]["discriminator"][
        "mapping"
    ]


def test_default_set_of_documents():
    docs = build_schemas()
    assert set(docs) >= {
        "unix-host",
        "embedded-host",
        "lab",
        "link",
        "settings",
        "reservations",
        "inventory",
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


def test_lab_schema_v2_sections():
    lab = build_schemas(builtins_only=True)["lab"]
    assert lab["type"] == "object"
    assert set(lab["properties"]) == {"$schema", "labs", "elements", "links"}
    assert lab["additionalProperties"] is False
    assert "^_" in lab["patternProperties"]
    labs = lab["properties"]["labs"]
    assert labs["type"] == "object"
    assert labs["additionalProperties"] == {"$ref": "#/$defs/LabEntrySpec"}
    assert set(lab["$defs"]["LabEntrySpec"]["properties"]) == {"resources", "metadata"}
    elements = lab["properties"]["elements"]
    assert elements["type"] == "array"
    el = lab["$defs"]["ElementSpec"]
    assert set(el["properties"]) >= {"name", "id", "labs", "metadata", "hosts"}
    hosts = el["properties"]["hosts"]
    # anyOf, not oneOf — minimal hosts validate against >1 spec.
    assert "anyOf" in hosts["items"]
    assert "oneOf" not in hosts["items"]
    assert {ref["$ref"] for ref in hosts["items"]["anyOf"]} == {
        "#/$defs/UnixHostSpec",
        "#/$defs/EmbeddedHostSpec",
    }
    assert hosts["items"]["discriminator"]["propertyName"] == "os_type"
    # Every registered os_type name is mapped to its spec's $def — `zephyr` and
    # `embedded` are two names for the one spec.
    assert hosts["items"]["discriminator"]["mapping"] == {
        "unix": "#/$defs/UnixHostSpec",
        "embedded": "#/$defs/EmbeddedHostSpec",
        "zephyr": "#/$defs/EmbeddedHostSpec",
    }


def test_host_specs_have_no_labs_but_metadata_and_resources():
    """``labs`` is the element's; ``metadata`` and ``resources`` are the host's own.

    These are the STANDALONE per-spec documents, which describe the flat host
    dict — so ``element``/``element_id`` legitimately survive here even though a
    v2 host ENTRY may not carry them (see
    ``test_element_host_entries_drop_the_hoisted_keys``). ``resources`` returned
    to ``HostSpec`` with spec 2026-08-28 three-level-reservations and so appears
    at both places.
    """
    docs = build_schemas(builtins_only=True)
    for stem in ("unix-host", "embedded-host"):
        props = set(docs[stem]["properties"])
        assert "metadata" in props
        assert "resources" in props
        assert "labs" not in props


def test_resources_are_offered_at_element_and_host_level():
    """Spec 2026-08-28 three-level-reservations §8: all three levels come from the models.

    ``_drop_hoisted_keys`` is driven off ``HOISTED_HOST_KEYS``, so ``resources``
    leaving that set is the whole edit — the nested host entry offers it without
    a schema-builder change.
    """
    lab = build_schemas(builtins_only=True)["lab"]
    assert "resources" in lab["$defs"]["LabEntrySpec"]["properties"]
    assert "resources" in lab["$defs"]["ElementSpec"]["properties"]
    host_defs = [n for n in lab["$defs"] if n.endswith("HostSpec")]
    assert host_defs, sorted(lab["$defs"])  # never let the loop below be vacuous
    for name in host_defs:
        assert "resources" in lab["$defs"][name]["properties"], name


def test_valid_impairers_enum_injected():
    prop = build_schemas(builtins_only=True)["unix-host"]["properties"]["valid_impairers"]
    enums = [b["enum"] if b["type"] == "string" else b["items"]["enum"] for b in prop["anyOf"]]
    assert all("netem" in e for e in enums)


def test_version_stamp_present():
    from otto.version import get_version

    for doc in build_schemas().values():
        assert doc["x-otto-version"] == get_version()


def test_element_host_entries_drop_the_hoisted_keys():
    """A host entry nested in an element may not carry ``element``/``element_id``.

    ``ElementSpec`` rejects every ``HOISTED_HOST_KEYS`` member inside a host
    entry (they live on the element / the ``labs`` table now), so the nested
    host sub-schemas must neither require nor permit them — while the
    standalone per-spec documents, which describe the FLAT host dict the
    factory still takes, keep them.
    """
    from otto.models.lab import HOISTED_HOST_KEYS

    docs = build_schemas(builtins_only=True)
    defs = docs["lab"]["$defs"]
    for name in ("UnixHostSpec", "EmbeddedHostSpec"):
        props = set(defs[name]["properties"])
        assert not props & HOISTED_HOST_KEYS, name
        assert not set(defs[name].get("required", [])) & HOISTED_HOST_KEYS, name
        assert defs[name]["additionalProperties"] is False  # so they are REJECTED, not ignored
    # The flat-dict documents are unchanged: element identity still belongs there.
    assert "element" in docs["unix-host"]["properties"]


def test_link_schema_emitted():
    docs = build_schemas(builtins_only=True)
    link = docs["link"]
    assert link["title"] == "otto link"
    assert "endpoints" in link["properties"]


def test_custom_registered_spec_appears(monkeypatch):
    # A custom host class + spec registered at runtime must flow into both its
    # own file and the hosts wrapper, without touching the real registry.
    class AcmeSpec(HostSpec):
        pass

    monkeypatch.setitem(op._HOST_SPECS, "acme", AcmeSpec)
    docs = build_schemas()
    mapping = _element_host_discriminator_mapping(docs)
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
    assert "acme" in _element_host_discriminator_mapping(full)

    builtins = build_schemas(builtins_only=True)
    assert "acme" not in builtins
    mapping = _element_host_discriminator_mapping(builtins)
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
        assert vx["anyOf"][0]["enum"] == ["ftp", "nc", "scp", "sftp", "shell"]
        assert vx["anyOf"][1]["items"]["enum"] == ["ftp", "nc", "scp", "sftp", "shell"]
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

    def test_element_host_defs_carry_enums(self):
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
            "shell",
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
        with pytest.raises(jsonschema.ValidationError, match=r"'bogus' is not one of"):
            jsonschema.validate("bogus", vt_schema)  # out-of-enum scalar
        with pytest.raises(jsonschema.ValidationError, match=r"'bogus' is not one of"):
            jsonschema.validate(["bogus"], vt_schema)  # out-of-enum in list


def test_monitor_export_schema_shape():
    docs = build_schemas(builtins_only=True)
    doc = docs["monitor-export"]
    assert doc["title"] == "Monitor historical export document"
    assert set(doc["required"]) == {"format", "sessions"}
    assert doc["properties"]["format"]["const"] == 1
    assert "TunnelRecord" in doc["$defs"]


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
        "tunnels",
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


def test_inventory_schema_is_the_record_keyed_by_inventory_key():
    doc = build_schemas()["inventory"]
    assert doc["$id"].endswith("/inventory.schema.json")
    assert doc["x-otto-version"]
    assert doc["additionalProperties"] == {"$ref": "#/$defs/InventoryRecord"}
    assert doc["properties"]["$schema"] == {"type": "string"}
    assert doc["patternProperties"]["^_"] == {}
    record = doc["$defs"]["InventoryRecord"]
    assert record["patternProperties"]["^_"] == {}
    assert "ip" in record["required"]
    # the interface shorthand ("eth0": "10.0.0.5") is accepted, as on host entries
    assert {"type": "string"} in record["properties"]["interfaces"]["additionalProperties"]["anyOf"]
