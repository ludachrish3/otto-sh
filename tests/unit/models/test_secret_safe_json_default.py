"""``secret_safe_json_default``: the one ``json.dumps`` hook over parsed settings data (#487)."""

import json

from pydantic import SecretStr

from otto.models.base import load_toml_settings, secret_safe_json_default


def _dump(secret: str) -> str:
    return json.dumps({"passphrase": SecretStr(secret)}, default=secret_safe_json_default)


def test_a_secret_dumps_as_a_stable_digest_that_tracks_the_secret():
    assert _dump("s3cret-a") == _dump("s3cret-a")
    assert _dump("s3cret-a") != _dump("s3cret-b")
    assert "s3cret" not in _dump("s3cret-a")


def test_anything_else_dumps_as_its_str():
    assert json.dumps({"x": 1.5j}, default=secret_safe_json_default) == '{"x": "1.5j"}'


def test_the_settings_parser_wraps_a_secret_at_any_depth():
    data = load_toml_settings('[a.b.c]\npassphrase = "p"\n[[d]]\npassword = "q"\n')
    assert isinstance(data["a"]["b"]["c"]["passphrase"], SecretStr)
    assert isinstance(data["d"][0]["password"], SecretStr)
