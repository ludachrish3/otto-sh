"""``secret_safe_json_default``: the one ``json.dumps`` hook over parsed settings data (#487).

A secret dumps as an HMAC under the install's key (``otto_home()/secret_salt``,
made by :func:`otto.config.home.secret_digest_key`). Each test gets a home of
its own: the hermetic one is shared by a whole worker, and these create,
replace and race the key file.
"""

import json
import os
import stat
import subprocess
import sys

import pytest
from pydantic import SecretStr

from otto.config.home import DIGEST_SALT_FILENAME, otto_home, secret_digest_key
from otto.models.base import load_toml_settings, secret_safe_json_default


@pytest.fixture(autouse=True)
def _own_home(monkeypatch, tmp_path):
    from otto.config import home

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(home, "_PROCESS_DIGEST_KEY", [])  # no fallback key carried in


def _dump(secret: str) -> str:
    hook = secret_safe_json_default(secret_digest_key)
    return json.dumps({"passphrase": SecretStr(secret)}, default=hook)


def test_a_secret_dumps_as_a_keyed_digest_that_tracks_the_secret():
    first = _dump("s3cret-a")
    assert first == _dump("s3cret-a")
    assert first != _dump("s3cret-b")
    assert "s3cret" not in first
    assert '"secret:hmac-sha256:' in first


def test_the_digest_is_keyed_by_the_install_key(monkeypatch, tmp_path):
    """Another install (another key) digests the same secret differently: an
    unkeyed hash would let anyone holding a stored digest test guesses."""
    here = _dump("s3cret-a")
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "other-install"))
    assert _dump("s3cret-a") != here


def test_the_digest_is_stable_across_processes():
    script = (
        "import json\n"
        "from pydantic import SecretStr\n"
        "from otto.config.home import secret_digest_key\n"
        "from otto.models.base import secret_safe_json_default\n"
        "hook = secret_safe_json_default(secret_digest_key)\n"
        "print(json.dumps({'passphrase': SecretStr('s3cret-a')}, default=hook))\n"
    )
    here = _dump("s3cret-a")  # made first, so the child reads this key
    child = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert child.stdout.strip() == here


def test_the_key_file_is_private_and_made_once():
    key = secret_digest_key()
    path = otto_home() / DIGEST_SALT_FILENAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(key) == 32
    assert secret_digest_key() == key


def test_a_racing_creator_returns_the_winners_key(monkeypatch):
    """The loser of the exclusive create reads the file the winner made."""
    import otto.utils

    real = otto.utils.atomic_write_bytes
    winner = os.urandom(32)

    def raced(path, payload, **kw):
        real(path, winner, exclusive=True)  # the other process lands first
        real(path, payload, **kw)

    monkeypatch.setattr(otto.utils, "atomic_write_bytes", raced)
    assert secret_digest_key() == winner


@pytest.mark.parametrize("content", [b"", b"x"])
def test_a_key_file_that_is_not_32_bytes_is_made_again(content):
    path = otto_home() / DIGEST_SALT_FILENAME
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    key = secret_digest_key()
    assert len(key) == 32
    assert path.read_bytes() == key


def test_a_filesystem_without_hard_links_still_keeps_the_key(monkeypatch):
    """``os.link`` (the exclusive create) refused: a plain atomic write keeps it."""

    def no_links(*_args, **_kw):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_links)
    key = secret_digest_key()
    assert (otto_home() / DIGEST_SALT_FILENAME).read_bytes() == key
    assert secret_digest_key() == key


def test_a_home_that_cannot_hold_the_key_gets_one_per_process(monkeypatch, caplog):
    """Both writes refused: a key for this process, stable, with one warning."""
    import otto.utils
    from otto.config import home

    def refused(*_args, **_kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(home, "_PROCESS_DIGEST_KEY", [])
    monkeypatch.setattr(otto.utils, "atomic_write_bytes", refused)
    with caplog.at_level("WARNING", logger="otto.config.home"):
        first = secret_digest_key()
        second = secret_digest_key()
    assert first == second
    assert len(first) == 32
    assert not (otto_home() / DIGEST_SALT_FILENAME).exists()
    warnings = [r for r in caplog.records if r.name == "otto.config.home"]
    assert len(warnings) == 1
    assert "this process" in warnings[0].getMessage()


def test_a_dump_reads_the_key_at_most_once():
    calls: list[int] = []

    def counted() -> bytes:
        calls.append(1)
        return b"k" * 32

    hook = secret_safe_json_default(counted)
    json.dumps({"a": SecretStr("1"), "b": [SecretStr("2"), {"c": SecretStr("3")}]}, default=hook)
    assert calls == [1]


def test_a_dump_without_a_secret_never_makes_the_key():
    """Through the real key source: nothing secret, so ``secret_salt`` is never made."""
    from datetime import timedelta

    from otto.inventory.cache import snapshot_slug_material

    snapshot_slug_material("netbox", "netbox:x", {"url": "https://nb"}, cache_ttl=timedelta(0))
    assert not (otto_home() / DIGEST_SALT_FILENAME).exists()


def test_the_settings_parser_wraps_a_secret_at_any_depth():
    data = load_toml_settings('[a.b.c]\npassphrase = "p"\n[[d]]\npassword = "q"\n')
    assert isinstance(data["a"]["b"]["c"]["passphrase"], SecretStr)
    assert isinstance(data["d"][0]["password"], SecretStr)


def test_no_home_to_resolve_gets_the_per_process_key(monkeypatch):
    """``Path.home()`` raises ``RuntimeError`` with no HOME and no passwd entry."""
    from otto.config import home

    def no_home():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(home, "otto_home", no_home)
    key = secret_digest_key()
    assert len(key) == 32
    assert secret_digest_key() == key


def test_a_directory_in_the_keys_place_gets_the_per_process_key():
    path = otto_home() / DIGEST_SALT_FILENAME
    path.mkdir(parents=True)
    key = secret_digest_key()
    assert len(key) == 32
    assert secret_digest_key() == key
    assert path.is_dir()
