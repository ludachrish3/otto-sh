"""A lab file, creds store, NetBox record or settings file never logs its secrets (#487).

A load failure is written to both log files with every frame's locals
(:func:`otto.logger.management.record_command_failure`). Before a model wraps
it, a password is a plain ``str`` in the parsed document, and the parser's own
frames (``json``'s, ``tomli``'s) hold the whole file text. Each case plants a
secret, forces the load to fail (a validation error, and a decode error in the
same file), renders the traceback as the log files do, and finds the secret
nowhere.
"""

import logging
import textwrap

import pytest
from pydantic import SecretStr, ValidationError

from otto.config.project import Project
from otto.creds import JsonCredsStore
from otto.creds.errors import CredsError
from otto.inventory import InventoryError
from otto.inventory.netbox import NetBoxInventory
from otto.labs.errors import LabRepositoryError
from otto.labs.json_repository import JsonFileLabRepository
from otto.logger.formatters import capture_exception, render_traceback
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.project_scaffold import make_project
from tests.unit.inventory.netbox_stub import TOKEN, NetBoxStub, device

PLANTED = "PLANTED-SECRET-7f3a"


def _logged(exc: BaseException) -> str:
    """Render *exc* as a log file does: the captured Rich trace, every frame's locals."""
    record = logging.LogRecord(
        "otto", logging.ERROR, __file__, 0, "load failed", None, (type(exc), exc, exc.__traceback__)
    )
    capture_exception(record)
    return render_traceback(record, color=False)


_CREDS = [{"login": "u", "password": PLANTED}]


def test_a_lab_host_that_fails_to_validate_logs_no_password(tmp_path):
    hosts = [{"ip": "10.0.0.9", "creds": _CREDS, "bogus": 1, "element": "bad", "labs": ["l"]}]
    write_lab_json(tmp_path / "lab.json", hosts)
    with pytest.raises(LabRepositoryError) as exc:
        JsonFileLabRepository(search_paths=[tmp_path]).load_lab("l")
    text = _logged(exc.value)
    assert "host_data" in text  # the frames holding the parsed entry are shown, locals and all
    assert PLANTED not in text


def test_a_lab_file_that_fails_to_parse_logs_no_password(tmp_path):
    (tmp_path / "lab.json").write_text(
        f'{{"_c": {{"password": "{PLANTED}"}}, "elements": [ oops ]}}'
    )
    with pytest.raises(LabRepositoryError, match="malformed JSON") as exc:
        JsonFileLabRepository(search_paths=[tmp_path]).load_lab("l")
    text = _logged(exc.value)
    assert "decoder.py" in text  # json's frames are listed, by file and line only
    assert PLANTED not in text


def test_a_creds_store_entry_that_fails_to_validate_logs_no_password(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text(f'{{"k": [{{"login": "u", "password": "{PLANTED}", "via": "x"}}]}}')
    with pytest.raises(CredsError) as exc:
        JsonCredsStore(path).lookup("k")
    text = _logged(exc.value)
    assert "parse_creds_document" in text
    assert PLANTED not in text


def test_a_creds_store_that_fails_to_parse_logs_no_password(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text(f'{{"k": [{{"login": "u", "password": "{PLANTED}"}}] oops')
    with pytest.raises(CredsError) as exc:
        JsonCredsStore(path).lookup("k")
    text = _logged(exc.value)
    assert "decoder.py" in text
    assert PLANTED not in text


def test_a_netbox_record_that_fails_to_validate_logs_no_password(monkeypatch):
    monkeypatch.setenv("NETBOX_TOKEN", TOKEN)
    d = device(1, "d1", custom_fields={"password": PLANTED, "slot_cf": "not a number"})
    with NetBoxStub([d]) as stub:
        inv = NetBoxInventory(
            url=stub.base, custom_fields={"shelf": "slot_cf"}, extra_custom_fields=["password"]
        )
        with pytest.raises(InventoryError) as exc:
            inv.lookup("d1")
    text = _logged(exc.value)
    assert "_record_for" in text
    assert PLANTED not in text


def _root_with_passphrase(tmp_path, ssh_options_tail: str = ""):
    """Write a project whose settings plant :data:`PLANTED` as a nested key passphrase.

    Returns the root only, so the test's own frame (shown in the rendered
    traceback) holds no copy of the file text: what is asserted is what the
    product's frames hold.
    """
    extra = textwrap.dedent(f"""
        [host_preferences.".*".ssh_options]
        passphrase = "{PLANTED}"
        {ssh_options_tail}
    """)
    return make_project(tmp_path, name="p", extra=extra)


def test_a_settings_passphrase_is_wrapped_when_the_file_parses(tmp_path):
    project = Project(root=_root_with_passphrase(tmp_path))
    value = project.settings["host_preferences"][".*"]["ssh_options"]["passphrase"]
    assert isinstance(value, SecretStr)
    assert value.get_secret_value() == PLANTED


def test_a_settings_file_that_fails_to_validate_logs_no_passphrase(tmp_path):
    root = _root_with_passphrase(tmp_path, 'port = "not a port"')
    with pytest.raises(ValidationError, match="port") as exc:
        Project(root=root)
    text = _logged(exc.value)
    assert "parse_settings" in text  # the frame holding the parsed table is shown, locals and all
    assert PLANTED not in text


def test_a_settings_file_that_fails_to_parse_logs_no_passphrase(tmp_path):
    import tomli

    root = _root_with_passphrase(tmp_path, "oops = = 1")
    with pytest.raises(tomli.TOMLDecodeError, match="line 7") as exc:
        Project(root=root)
    text = _logged(exc.value)
    assert "load_toml_settings" in text  # the raise point is shown, its locals without the text
    assert PLANTED not in text
