"""~/.otto/settings.toml — the user-level settings file (spec §8)."""

import pytest

from otto.config.user_settings import (
    CACHE_ENABLED_KEY,
    CACHE_TABLE,
    CacheSetting,
    _memo,
    cache_enabled,
    clear_user_settings_memo,
    load_user_settings,
    user_settings_path,
)
from otto.models.settings import CacheSettingsSpec, UserSettingsModel


def _user_file(dir_, body=""):
    """Write the USER-level settings file — the subject of every test here.

    Not a project: ``make_project`` writes ``<root>/.otto/settings.toml``
    inside a project, which is a different file that happens to share a
    basename. One writer, so the scaffold-policy exemption is stated once.
    """
    settings_file = dir_ / "settings.toml"
    settings_file.write_text(body)  # scaffold-exempt: user-level ~/.otto file, not a project
    return settings_file


def test_path_follows_otto_home(tmp_path, monkeypatch):
    # tests/conftest.py strips OTTO_* at IMPORT time, so setting it here is honoured.
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    assert user_settings_path() == tmp_path / "settings.toml"


def test_path_is_pure_and_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "absent"))
    assert user_settings_path() == tmp_path / "absent" / "settings.toml"
    assert not (tmp_path / "absent").exists()


def test_absent_file_is_none(tmp_path):
    assert load_user_settings(tmp_path / "settings.toml") is None


def test_parses_inventory_table(tmp_path):
    p = _user_file(tmp_path, '[inventory]\nbackend = "json"\npath = "i.json"\n')
    model = load_user_settings(p)
    assert model is not None
    assert model.inventory is not None
    assert model.inventory.backend == "json"
    assert model.inventory.model_extra == {"path": "i.json"}


def test_an_empty_file_parses_to_a_model_with_no_inventory(tmp_path):
    p = _user_file(tmp_path)
    model = load_user_settings(p)
    assert model is not None
    assert model.inventory is None


def test_a_project_only_table_errors_naming_the_key_and_file(tmp_path):
    p = _user_file(tmp_path, '[reservations]\nbackend = "none"\n')
    with pytest.raises(
        ValueError,
        match=r"settings\.toml: [\s\S]*reservations: Extra inputs are not permitted",
    ):
        load_user_settings(p)


def test_a_broken_file_errors_naming_the_file(tmp_path):
    p = _user_file(tmp_path, "[inventory\n")
    with pytest.raises(
        ValueError, match=r"settings\.toml: Expected '\]' at the end of a table declaration"
    ):
        load_user_settings(p)


def test_default_path_is_used_when_none_is_passed(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    assert load_user_settings() is None
    _user_file(tmp_path, '[inventory]\nbackend = "json"\npath = "i.json"\n')
    clear_user_settings_memo()  # the absent file was memoized
    model = load_user_settings()
    assert model is not None
    assert model.inventory is not None
    assert model.inventory.backend == "json"


def test_parses_creds_table(tmp_path):
    p = _user_file(tmp_path, '[creds]\nbackend = "json"\npath = "c.json"\n')
    model = load_user_settings(p)
    assert model is not None
    assert model.creds is not None
    assert model.creds.backend == "json"
    assert model.creds.model_extra == {"path": "c.json"}
    assert model.inventory is None


def _own_file(body):
    """Write the default-path user settings file of this worker's shared hermetic OTTO_HOME."""
    p = user_settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)  # scaffold-exempt: user-level ~/.otto file, not a project
    return p


def test_cache_table_names_the_model_field():
    assert CACHE_TABLE in UserSettingsModel.model_fields
    assert CACHE_ENABLED_KEY in CacheSettingsSpec.model_fields


def test_cache_on_by_default_without_a_settings_file():
    assert cache_enabled() == CacheSetting(on=True, source="default")


def test_cache_on_by_default_without_a_cache_table():
    _own_file('[inventory]\nbackend = "json"\npath = "i.json"\n')
    assert cache_enabled() == CacheSetting(on=True, source="default")


def test_cache_table_without_enabled_is_on():
    _own_file("[cache]\n")
    assert cache_enabled() == CacheSetting(on=True, source=f"settings.toml: {user_settings_path()}")


def test_cache_disabled_by_the_settings_file():
    _own_file("[cache]\nenabled = false\n")
    assert cache_enabled() == CacheSetting(
        on=False, source=f"settings.toml: {user_settings_path()}"
    )


def test_unparsable_settings_resolve_off_with_the_error():
    _own_file("[cache\n")
    s = cache_enabled()
    assert not s.on
    assert str(user_settings_path()) in s.source


def test_invalid_cache_value_resolves_off_with_the_error():
    _own_file('[cache]\nenabled = "maybe"\n')
    s = cache_enabled()
    assert not s.on
    assert "enabled" in s.source


def test_unknown_cache_key_resolves_off():
    _own_file("[cache]\nenable = false\n")
    s = cache_enabled()
    assert not s.on
    assert "enable" in s.source
    assert "Extra inputs are not permitted" in s.source


def test_the_settings_file_is_read_once_per_process(monkeypatch):
    p = _own_file("[cache]\nenabled = false\n")
    reads = []
    real = type(p).read_bytes

    def spy(self, *a, **k):
        reads.append(self)
        return real(self, *a, **k)

    monkeypatch.setattr(type(p), "read_bytes", spy)
    cache_enabled()
    load_user_settings()
    cache_enabled()
    assert reads == [p]
    clear_user_settings_memo()
    cache_enabled()
    assert reads == [p, p]


def test_an_absent_file_is_memoized_and_an_error_is_not(monkeypatch):
    assert cache_enabled().on
    _own_file("[cache]\nenabled = false\n")
    assert cache_enabled().on  # the memoized None still answers
    clear_user_settings_memo()
    _own_file("[cache\n")
    assert not cache_enabled().on
    _own_file("[cache]\nenabled = true\n")  # a broken read left nothing behind
    assert cache_enabled().on


def test_an_explicit_path_bypasses_the_memo(tmp_path):
    assert load_user_settings() is None
    other = _user_file(tmp_path, "[cache]\nenabled = false\n")
    first = load_user_settings(other)
    assert first is not None
    assert first.cache is not None
    assert not first.cache.enabled
    other.write_text("[cache]\nenabled = true\n")  # scaffold-exempt: user-level file
    second = load_user_settings(other)
    assert second is not None
    assert second.cache is not None
    assert second.cache.enabled


def test_the_memo_holds_one_entry_across_homes(tmp_path, monkeypatch):
    for name in ("a", "b"):
        monkeypatch.setenv("OTTO_HOME", str(tmp_path / name))
        assert load_user_settings() is None
    assert list(_memo) == [tmp_path / "b" / "settings.toml"]


# The resolver, which also owns the tomlkit edit (`set_cache_enabled`).
_CACHE_TABLE_READERS = {"config/user_settings.py"}
# The only file that may call cache_enabled(): the one switch (the resolver only defines it).
_CACHE_ENABLED_CALLERS = {
    "config/completion_cache.py",
    # set_cache_enabled returns the setting the next caller will see; it decides nothing.
    "config/user_settings.py",
    # `otto cache info` reports the resolved setting on its first line; it decides nothing.
    "cli/cache.py",
}


def _src_files():
    from pathlib import Path

    import otto

    src = Path(otto.__file__).parent
    for f in sorted(src.rglob("*.py")):
        yield f.relative_to(src).as_posix(), f


def test_only_the_resolver_reads_the_cache_table():
    import re

    pattern = re.compile(
        r"\.cache\.enabled|\bCACHE_TABLE\b|\bCACHE_ENABLED_KEY\b"
        r"""|\[\s*["']cache["']\s*\]|\.get\(\s*["']cache["']"""
    )
    matched = {rel for rel, f in _src_files() if pattern.search(f.read_text())}
    assert "config/user_settings.py" in matched  # the guard sees the resolver
    assert matched <= _CACHE_TABLE_READERS


def test_only_the_one_switch_calls_cache_enabled():
    import ast

    def called(node):
        fn = node.func
        return (isinstance(fn, ast.Name) and fn.id == "cache_enabled") or (
            isinstance(fn, ast.Attribute) and fn.attr == "cache_enabled"
        )

    callers = {
        rel
        for rel, f in _src_files()
        if any(isinstance(n, ast.Call) and called(n) for n in ast.walk(ast.parse(f.read_text())))
    }
    assert "config/completion_cache.py" in callers  # the guard sees the one switch
    assert callers <= _CACHE_ENABLED_CALLERS


def test_the_settings_stat_is_taken_once_and_survives_a_broken_file():
    from otto.config.user_settings import (
        clear_user_settings_memo,
        load_user_settings,
        user_settings_path,
        user_settings_stat,
    )

    assert user_settings_stat() is None  # absent
    path = user_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[cache\n")  # scaffold-exempt: user-level ~/.otto file
    assert user_settings_stat() is None  # memoized: the later write is not seen
    clear_user_settings_memo()
    with pytest.raises(ValueError, match=r"settings\.toml"):
        load_user_settings()
    st = user_settings_stat()
    assert st is not None  # recorded though parsing failed
    assert st.st_size == path.stat().st_size
    assert user_settings_stat() is st


def _assert_off_with_one_line_error(path):
    """The file is broken: the loader names it, and the cache resolves off with that error."""
    from otto.config.user_settings import cache_enabled, user_settings_stat

    with pytest.raises(ValueError, match=r"settings\.toml") as ei:
        load_user_settings()
    assert "\n" not in str(ei.value)
    assert str(ei.value).startswith(f"{path}: ")
    setting = cache_enabled()
    assert setting.on is False
    assert setting.source == str(ei.value)
    assert user_settings_stat() is None


def test_a_self_referencing_settings_symlink_is_broken_not_a_crash():
    path = user_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(path)  # ELOOP on stat
    _assert_off_with_one_line_error(path)


def _private_home(tmp_path, monkeypatch):
    """A home of this test's own: the shared one's teardown unlinks settings.toml, not a dir."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    return user_settings_path()


def test_a_settings_path_that_is_a_directory_is_broken(tmp_path, monkeypatch):
    path = _private_home(tmp_path, monkeypatch)
    path.mkdir(parents=True)
    _assert_off_with_one_line_error(path)


def test_a_settings_path_that_is_a_fifo_is_broken_and_never_opened(tmp_path, monkeypatch):
    import os

    path = _private_home(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)  # opening it for read would block forever
    _assert_off_with_one_line_error(path)


def test_a_settings_file_that_cannot_be_stat_is_broken(tmp_path, monkeypatch):
    import os

    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    locked = tmp_path / "locked"
    monkeypatch.setenv("OTTO_HOME", str(locked / "home"))
    path = user_settings_path()
    path.parent.mkdir(parents=True)
    path.write_text("[cache]\nenabled = true\n")  # scaffold-exempt: user-level ~/.otto file
    locked.chmod(0)  # EACCES on stat
    try:
        _assert_off_with_one_line_error(path)
    finally:
        locked.chmod(0o700)


def test_an_absent_parent_component_that_is_a_file_is_absent(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    monkeypatch.setenv("OTTO_HOME", str(blocker))  # ENOTDIR on stat
    assert cache_enabled() == CacheSetting(on=True, source="default")


_ORIGINAL = (
    '# my settings\n[inventory]\nbackend = "file"  # keep\n\n[creds]\nbackend = "json"  # note\n'
)


@pytest.mark.parametrize("original", [_ORIGINAL, _ORIGINAL.rstrip("\n")])
def test_disable_then_enable_round_trips_the_rest_of_the_file(original):
    from otto.config.user_settings import set_cache_enabled

    path = _own_file(original)
    assert not set_cache_enabled(False).on
    off = path.read_text()
    assert "enabled = false" in off
    assert set_cache_enabled(True).on
    if original.endswith("\n"):
        assert path.read_text() == original + "\n[cache]\nenabled = true\n"
    else:
        assert path.read_text().startswith(original)
        assert path.read_text().endswith("[cache]\nenabled = true\n")


def test_disable_creates_the_home_and_the_file(tmp_path, monkeypatch):
    from otto.config.user_settings import set_cache_enabled

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "missing" / "home"))
    s = set_cache_enabled(False)
    assert user_settings_path().read_text() == "[cache]\nenabled = false\n"
    assert not s.on


def test_enable_without_a_cache_table_writes_nothing():
    from otto.config.user_settings import set_cache_enabled

    assert set_cache_enabled(True).on
    assert not user_settings_path().exists()


def test_enable_keeps_a_file_without_a_cache_table_byte_identical():
    from otto.config.user_settings import set_cache_enabled

    path = _own_file(_ORIGINAL)
    set_cache_enabled(True)
    assert path.read_text() == _ORIGINAL


def test_set_refuses_a_settings_path_that_is_a_directory(tmp_path, monkeypatch):
    from otto.config.user_settings import set_cache_enabled

    path = _private_home(tmp_path, monkeypatch)
    path.mkdir(parents=True)
    for wanted in (False, True):
        with pytest.raises(ValueError, match=r"settings\.toml"):
            set_cache_enabled(wanted)
        assert path.is_dir()


def test_an_idempotent_set_still_reports_an_external_edit():
    from otto.config.user_settings import set_cache_enabled

    assert cache_enabled().on  # memoizes the absent file
    _own_file("[cache]\nenabled = false\n")  # edited behind the memo's back
    assert not set_cache_enabled(False).on


def test_enable_on_a_bare_cache_table_writes_the_key():
    from otto.config.user_settings import set_cache_enabled

    path = _own_file("[cache]\n")
    assert set_cache_enabled(True).on
    assert path.read_text() == "[cache]\nenabled = true\n"


def test_a_crlf_file_keeps_its_line_endings():
    from otto.config.user_settings import set_cache_enabled

    path = user_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = b'# mine\r\n[cache]\r\nenabled = false  # why\r\n\r\n[creds]\r\nbackend = "json"\r\n'
    path.write_bytes(body)
    assert set_cache_enabled(True).on
    assert path.read_bytes() == body.replace(b"enabled = false", b"enabled = true")


def test_set_is_idempotent():
    import os

    from otto.config.user_settings import set_cache_enabled

    path = user_settings_path()
    set_cache_enabled(False)
    os.utime(path, (1_000_000, 1_000_000))
    before = path.stat().st_mtime_ns
    assert not set_cache_enabled(False).on
    assert path.stat().st_mtime_ns == before


@pytest.mark.parametrize("body", ["[cache\n", '[cache]\nenabled = "maybe"\n'])
def test_a_broken_file_is_refused_unchanged(body):
    from otto.config.user_settings import set_cache_enabled

    path = _own_file(body)
    before = path.read_bytes()
    for wanted in (False, True):
        with pytest.raises(ValueError, match=r"settings\.toml"):
            set_cache_enabled(wanted)
        assert path.read_bytes() == before


def test_the_rewrite_keeps_the_file_mode():
    from otto.config.user_settings import set_cache_enabled

    path = _own_file(_ORIGINAL)
    path.chmod(0o640)
    set_cache_enabled(False)
    assert path.stat().st_mode & 0o777 == 0o640


def test_set_drops_the_memo_so_the_next_read_sees_it():
    from otto.config.user_settings import cache_enabled, set_cache_enabled

    assert cache_enabled().on  # memoizes the absent file
    set_cache_enabled(False)
    assert not cache_enabled().on


def test_tomlkit_is_not_imported_by_cache_enabled():
    import subprocess
    import sys

    code = (
        "import sys; from otto.config.user_settings import cache_enabled; cache_enabled(); "
        "sys.exit(1 if 'tomlkit' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_a_commented_cache_table_changes_only_the_value_token():
    from otto.config.user_settings import set_cache_enabled

    path = _own_file("[cache]  # hdr\nenabled = false  # why\n")
    assert set_cache_enabled(True).on
    assert path.read_text() == "[cache]  # hdr\nenabled = true  # why\n"


@pytest.mark.parametrize("raw", ["false", "no", "off", "0"])
def test_enable_rewrites_a_coerced_string_as_a_boolean(raw):
    from otto.config.user_settings import set_cache_enabled

    path = _own_file(f'[cache]\nenabled = "{raw}"\n')
    assert not cache_enabled().on  # pydantic coerces it off
    assert set_cache_enabled(True).on
    assert path.read_text() == "[cache]\nenabled = true\n"


def test_disable_rewrites_a_coerced_string_as_a_boolean():
    from otto.config.user_settings import set_cache_enabled

    path = _own_file('[cache]\nenabled = "false"\n')
    assert not set_cache_enabled(False).on
    assert path.read_text() == "[cache]\nenabled = false\n"


def test_a_validation_error_is_one_line_without_the_input():
    path = _own_file('[cache]\nenabled = "maybe-secret"\n')
    with pytest.raises(ValueError, match=r"settings\.toml") as ei:
        load_user_settings(path)
    text = str(ei.value)
    assert "\n" not in text
    assert "maybe-secret" not in text
    assert "pydantic.dev" not in text
    assert "cache.enabled" in text


def test_an_error_without_a_location_reads_as_the_shared_compact_form(monkeypatch):
    path = _own_file("")
    # A model-level validator reports loc == (); drive that through the formatter.
    from pydantic import BaseModel, ValidationError, model_validator

    class Probe(BaseModel):
        @model_validator(mode="after")
        def _always(self):
            raise ValueError("cache and inventory disagree")

    def validate(cls, data):
        return Probe.model_validate(data)

    monkeypatch.setattr(UserSettingsModel, "model_validate", classmethod(validate))
    with pytest.raises(ValueError, match=r"settings\.toml") as ei:
        load_user_settings(path)
    assert isinstance(ei.value.__cause__, ValidationError)
    assert str(ei.value).endswith(
        "settings.toml: <entry>: Value error, cache and inventory disagree [type=value_error]"
    )
