"""``scripts/api_manifest.py``: api/public.toml lists namespaces, never names."""

import json
import re

import pytest

from scripts.api_manifest import (
    Format,
    ManifestError,
    Namespace,
    load_formats,
    load_manifest,
    manifest_breaks,
    parse_formats,
    parse_manifest,
)

pytestmark = pytest.mark.interpreter_agnostic

GOOD = """
[namespaces."otto"]
tier = 1
stability = "provisional"

[namespaces."otto.host.transfer"]
tier = 2
stability = "stable"
pending = "promotion spec"
"""


def test_parses_namespaces_with_defaults():
    got = parse_manifest(GOOD)
    assert got["otto"] == Namespace("otto", 1, "provisional", "")
    assert got["otto.host.transfer"] == Namespace(
        "otto.host.transfer", 2, "stable", "promotion spec"
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not toml [", "not valid TOML"),
        ("", "no [namespaces] table"),
        ('[namespaces."otto"]\ntier = 3\nstability = "provisional"\n', "tier must be one of"),
        ('[namespaces."otto"]\ntier = 1\nstability = "frozen"\n', "stability must be one of"),
        ('[namespaces."otto"]\ntier = 1\nstability = "stable"\nnames = ["A"]\n', "unknown key"),
        ('[namespaces."json"]\ntier = 1\nstability = "stable"\n', "not an otto module path"),
        ('extra = 1\n[namespaces."otto"]\ntier = 1\nstability = "stable"\n', "unknown top-level"),
        (
            '[namespaces."otto"]\ntier = 1\nstability = "stable"\npending = 3\n',
            "pending must be a string",
        ),
    ],
)
def test_refuses_malformed_manifests(text, message):
    with pytest.raises(ManifestError, match=re.escape(message)):
        parse_manifest(text)


def test_a_missing_file_is_a_manifest_error(tmp_path):
    with pytest.raises(ManifestError, match="does not exist"):
        load_manifest(tmp_path / "public.toml")


CR = chr(13)
FORMATS_TOML = GOOD + '\n[formats.store]\nreads = "otto.versions:READS"\n'


@pytest.mark.parametrize("load", [load_manifest, load_formats])
def test_a_lone_cr_manifest_is_refused_as_the_checker_refuses_it(tmp_path, load):
    # The checker reads a committed manifest's bytes untranslated; a lone CR is
    # no TOML newline. A newline-translating read would repair it here only.
    path = tmp_path / "public.toml"
    path.write_bytes(FORMATS_TOML.replace("\n", CR).encode())
    with pytest.raises(ManifestError):
        load(path)


@pytest.mark.parametrize("load", [load_manifest, load_formats])
def test_a_crlf_manifest_is_valid_toml(tmp_path, load):
    path = tmp_path / "public.toml"
    path.write_bytes(FORMATS_TOML.replace("\n", CR + "\n").encode())
    assert load(path)


@pytest.mark.parametrize("load", [load_manifest, load_formats])
def test_a_non_utf8_manifest_is_a_manifest_error(tmp_path, load):
    path = tmp_path / "public.toml"
    path.write_bytes(FORMATS_TOML.encode() + b"# \xff\n")
    with pytest.raises(ManifestError, match="not UTF-8"):
        load(path)


def test_removal_and_downgrade_are_breaks_promotion_and_addition_are_not():
    parent = parse_manifest(GOOD)
    current = {
        "otto": Namespace("otto", 1, "stable"),  # promoted: free
        "otto.host.transfer": Namespace("otto.host.transfer", 2, "provisional"),  # downgraded
        "otto.lab": Namespace("otto.lab", 1, "provisional"),  # added: free
    }
    assert manifest_breaks(parent, current) == [
        "stability downgraded: otto.host.transfer stable -> provisional"
    ]
    assert manifest_breaks(parent, {"otto": parent["otto"]}) == [
        "namespace removed: otto.host.transfer"
    ]


NS = '[namespaces."otto"]\ntier = 1\nstability = "provisional"\n'


def test_formats_are_parsed_as_pointers():
    text = NS + (
        '\n[formats.link-sentinel]\nreads = "otto.link.sentinel:READ_VERSIONS"\n'
        'writes = "otto.link.sentinel:WRITE_VERSIONS"\n'
        '\n[formats.reservations]\nreads = "otto.reservations:READ_VERSIONS"\n'
    )
    assert parse_formats(text) == {
        "link-sentinel": Format(
            "link-sentinel",
            "otto.link.sentinel:READ_VERSIONS",
            "otto.link.sentinel:WRITE_VERSIONS",
        ),
        "reservations": Format("reservations", "otto.reservations:READ_VERSIONS", None),
    }
    assert set(parse_manifest(text)) == {"otto"}


def test_a_manifest_without_formats_has_none():
    assert parse_formats(NS) == {}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (NS + '[formats.Link_Sentinel]\nreads = "otto.a:R"\n', "kebab-case"),
        (NS + "[formats.link]\n", "declares neither reads nor writes"),
        (NS + '[formats.link]\nreads = "otto.a:R"\nnote = "x"\n', "unknown key"),
        (NS + '[formats.link]\nreads = "json:R"\n', "not an otto pointer"),
        (NS + '[formats.link]\nreads = "otto.a"\n', "not an otto pointer"),
        (NS + "[formats.link]\nreads = 1\n", "not an otto pointer"),
        # a bare key must come BEFORE the first table, or TOML files it inside that table
        ('formats = "x"\n' + NS, "expected tables"),
    ],
)
def test_malformed_formats_are_refused_by_both_parsers(text, message):
    with pytest.raises(ManifestError, match=message):
        parse_formats(text)
    with pytest.raises(ManifestError, match=message):
        parse_manifest(text)


@pytest.mark.parametrize(
    "name",
    ["ottox", "otto-foo", "otto.a b", "otto.host/x", "otto.", "otto.host\n"],
)
def test_a_malformed_namespace_name_is_refused(name):
    text = "[namespaces." + json.dumps(name) + ']\ntier = 1\nstability = "provisional"\n'
    with pytest.raises(ManifestError, match="not an otto module path"):
        parse_manifest(text)


def test_a_pointer_must_match_whole():
    text = NS + '[formats.link]\nreads = "otto.a:R x"\n'
    with pytest.raises(ManifestError, match="not an otto pointer"):
        parse_formats(text)
