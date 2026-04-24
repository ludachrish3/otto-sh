"""Pure-unit tests for :mod:`otto.configmodule.completion_cache`.

Focus on the small guards and the option-serialization code path — the
subprocess coverage in ``test_completion_cache.py`` exercises the full stack
but is heavy; these tests run in milliseconds and pinpoint regressions.

Note: this module intentionally does NOT use ``from __future__ import
annotations`` — ``_serialize_options`` introspects ``Annotated[...]`` forms
at runtime, and PEP 563 would stringify them, making the serializer skip the
option entirely.
"""
import json
import time
from pathlib import Path
from typing import Annotated

import typer

from otto.configmodule import completion_cache as cc


def test_read_cache_returns_none_for_empty_repos(tmp_path: Path, monkeypatch) -> None:
    """Empty-repo fingerprints poison the cache if allowed; read must skip them."""
    monkeypatch.setenv('OTTO_XDIR', str(tmp_path))
    # Write a plausible-looking cache entry keyed on the empty fingerprint.
    cache_file = cc._cache_path()
    assert cache_file is not None
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({
        cc.compute_fingerprint([]): {
            'schema_version': cc.SCHEMA_VERSION,
            'generated_at': int(time.time()),
            'instructions': [{'name': 'poisoned', 'options': []}],
            'suites': [],
        },
    }))

    assert cc.read_cache([]) is None


def test_write_cache_skips_empty_repos(tmp_path: Path, monkeypatch) -> None:
    """Writing for empty repos must be a no-op — no file, no poisoned entry."""
    monkeypatch.setenv('OTTO_XDIR', str(tmp_path))
    cc.write_cache([], instructions=[{'name': 'x', 'options': []}], suites=[], hosts=[])
    assert not cc._cache_path().exists()  # type: ignore[union-attr]


def test_read_cache_rejects_schema_mismatch(tmp_path: Path, monkeypatch) -> None:
    """A cache with an older schema version is not consulted."""
    from unittest.mock import MagicMock

    fake_repo = MagicMock()
    fake_repo.sutDir = tmp_path / 'sut'
    fake_repo.sutDir.mkdir()
    (fake_repo.sutDir / '.otto').mkdir()
    (fake_repo.sutDir / '.otto' / 'settings.toml').write_text('')
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    fake_repo.labs = []

    monkeypatch.setenv('OTTO_XDIR', str(tmp_path))
    cache_file = cc._cache_path()
    cache_file.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    cache_file.write_text(json.dumps({  # type: ignore[union-attr]
        cc.compute_fingerprint([fake_repo]): {
            'schema_version': cc.SCHEMA_VERSION - 1,
            'generated_at': int(time.time()),
            'instructions': [],
            'suites': [],
        },
    }))

    assert cc.read_cache([fake_repo]) is None


def test_serialize_options_handles_supported_kinds() -> None:
    """Every kind in the type-map should produce a non-None schema."""

    def source(
        s: Annotated[str,   typer.Option('--s')] = '',
        i: Annotated[int,   typer.Option('--i')] = 0,
        f: Annotated[float, typer.Option('--f')] = 0.0,
        b: Annotated[bool,  typer.Option('--b/--no-b')] = False,
        p: Annotated[Path,  typer.Option('--p')] = Path('.'),
        l: Annotated[list[str], typer.Option('--l')] = [],
    ) -> None: ...

    schema = cc._serialize_options(source, command_name='source')
    assert schema is not None
    kinds = [entry['kind'] for entry in schema]
    assert kinds == ['str', 'int', 'float', 'bool', 'path', 'str_list']


def test_serialize_options_returns_none_on_unsupported() -> None:
    """An unsupported annotation drops the entire command schema."""
    from decimal import Decimal

    def source(
        ok: Annotated[str, typer.Option('--ok')] = '',
        bad: Annotated[Decimal, typer.Option('--bad')] = Decimal('0'),
    ) -> None: ...

    assert cc._serialize_options(source, command_name='source') is None


def test_clear_cache_returns_false_when_missing(tmp_path: Path, monkeypatch) -> None:
    """clear_cache reports False when there's nothing to remove."""
    monkeypatch.setenv('OTTO_XDIR', str(tmp_path))
    assert cc.clear_cache() is False


def test_clear_cache_removes_existing(tmp_path: Path, monkeypatch) -> None:
    """clear_cache unlinks a present cache file and reports True."""
    monkeypatch.setenv('OTTO_XDIR', str(tmp_path))
    path = cc._cache_path()
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{}')
    assert cc.clear_cache() is True
    assert not path.exists()
