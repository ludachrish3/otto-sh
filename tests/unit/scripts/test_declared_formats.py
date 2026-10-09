"""otto's declared versioned formats (dump spec §13): recordable, sampled, light.

The declaration's ``[formats.<name>]`` tables point at literal lists of the
versions otto reads and writes. This runs the real API-dump producer on those
pointers. It also checks two things the dump alone cannot show: every read
version has a frozen sample (and no sample outlives its version), and every
module a pointer names imports nothing beyond the standard library and its
own package chain.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import api_records, api_regen
from scripts.api_manifest import load_formats
from tests._fixtures.paths import PROJECT_ROOT, TESTS_ROOT

MANIFEST = PROJECT_ROOT / "api" / "public.toml"
SAMPLES = TESTS_ROOT / "_fixtures" / "formats"
FORMATS = load_formats(MANIFEST)

DECLARED = [
    "check-echo-sentinel",
    "check-report",
    "coverage-capture",
    "coverage-store",
    "coverage-tickets",
    "kmodcov-interface",
    "link-impairment-sentinel",
    "monitor-database",
    "monitor-export",
    "monitor-live-stream",
    "reservations-file",
    "tunnel-sentinel",
]
"""The "yes" rows of dump spec §13.4. A new format is added here and to the declaration together."""

_PROBE = (
    "import importlib, json, sys\n"
    "before = set(sys.modules)\n"
    "importlib.import_module(sys.argv[1])\n"
    "print(json.dumps(sorted(set(sys.modules) - before)))\n"
)


@pytest.fixture(scope="module")
def recorded() -> "dict[str, api_records.Record]":
    report = api_regen.run_child(
        Path(sys.executable), PROJECT_ROOT / "src", [], formats=list(FORMATS.values())
    )
    assert report["refusals"] == []
    assert report["provenance"] == []
    records = [api_records.parse_record(line) for line in report["records"]]
    return api_records.parse_dump(api_records.render_dump(records)).formats


def test_the_declaration_names_every_versioned_format():
    assert sorted(FORMATS) == DECLARED


def test_every_declared_format_is_recorded_without_a_refusal(recorded):
    assert sorted(recorded) == DECLARED


def _encoded(sample: Path) -> str:
    version = int(sample.stem) if sample.stem.isdigit() else sample.stem
    return api_records.encode_value(version, lambda _cls: None)


def test_every_sample_folder_belongs_to_a_format_otto_reads():
    """Removing a format deletes its samples in the same commit (dump spec §13.5).

    The per-format test below walks ``DECLARED``, so it cannot see a folder
    left behind for a format that is no longer declared. Only a read version
    has a sample, so a write-only format has no folder.
    """
    readers = sorted(name for name in DECLARED if FORMATS[name].reads)
    assert sorted(p.name for p in SAMPLES.iterdir() if p.is_dir()) == readers


@pytest.mark.parametrize("name", DECLARED)
def test_each_read_version_has_a_frozen_sample_and_no_sample_outlives_its_version(name, recorded):
    folder = SAMPLES / name
    samples = sorted(_encoded(p) for p in folder.iterdir()) if folder.is_dir() else []
    assert samples == sorted(api_records.split_list(recorded[name].fields[0]))


def _pointer_modules() -> "list[str]":
    return sorted({p.partition(":")[0] for f in FORMATS.values() for p in (f.reads, f.writes) if p})


@pytest.mark.parametrize("module", _pointer_modules())
def test_each_format_module_imports_only_the_standard_library(module):
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, module],
        capture_output=True,
        text=True,
        check=True,
        cwd=PROJECT_ROOT,
    )
    loaded = json.loads(out.stdout)
    parts = module.split(".")
    chain = {".".join(parts[: i + 1]) for i in range(len(parts))}
    otto_extra = [m for m in loaded if m.split(".")[0] == "otto" and m not in chain]
    foreign = [
        m
        for m in loaded
        if m.split(".")[0] != "otto" and m.split(".")[0] not in sys.stdlib_module_names
    ]
    assert (otto_extra, foreign) == ([], [])
