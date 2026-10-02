"""``resolve_monitor_tls``: one dashboard TLS declaration across repos, fail-loud.

Certificates are throwaway self-signed PEMs minted into tmp_path with the
openssl CLI, as in ``test_server_tls.py``.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.config.repo import MonitorSettings
from otto.monitor.errors import MonitorTlsError
from otto.monitor.tls import resolve_monitor_tls


def _repo(name: str, cert=None, key=None) -> SimpleNamespace:
    return SimpleNamespace(name=name, monitor_settings=MonitorSettings(tls_cert=cert, tls_key=key))


def _make_cert(tmp_path: Path) -> Path:
    """Mint a throwaway self-signed PEM (cert+key bundled into one file) via openssl.

    Same approach as ``tests/unit/monitor/test_server_tls.py``: universally
    present on the Linux targets this repo supports, no extra dev-dep needed.
    Cert and key are generated to separate files (openssl can't safely write
    both to one ``-out``/``-keyout`` path — the second write clobbers the
    first) then concatenated, exercising the ``tls_key is None`` "cert
    bundles the key" path documented on ``MonitorSettings``.
    """
    key, crt = tmp_path / "key.pem", tmp_path / "crt.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-sha256",
            "-days",
            "2",
            "-keyout",
            str(key),
            "-out",
            str(crt),
            "-subj",
            "/CN=127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    bundle = tmp_path / "cert.pem"
    bundle.write_text(crt.read_text() + key.read_text())
    return bundle


def test_no_declaration_is_plain_http():
    assert resolve_monitor_tls([_repo("a"), _repo("b")]) is None


def test_single_declaration_with_real_files_applies(tmp_path):
    cert = _make_cert(tmp_path)
    settings = resolve_monitor_tls([_repo("a", cert)])
    assert settings is not None
    assert settings.tls_cert == cert


def test_agreeing_repos_resolve_to_the_declaration(tmp_path):
    cert = _make_cert(tmp_path)
    settings = resolve_monitor_tls([_repo("a", cert), _repo("b", cert)])
    assert settings is not None
    assert settings.tls_cert == cert


def test_disagreeing_repos_refuse_naming_both(tmp_path):
    c1, c2 = tmp_path / "1.pem", tmp_path / "2.pem"
    with pytest.raises(MonitorTlsError, match=r"disagree across repos \(a, b\)") as excinfo:
        resolve_monitor_tls([_repo("b", c2), _repo("a", c1)])
    assert (excinfo.value.setting, excinfo.value.repos) == (None, ["a", "b"])


def test_missing_cert_file_refuses_naming_the_setting(tmp_path):
    missing = tmp_path / "nope.pem"
    with pytest.raises(MonitorTlsError, match="tls_cert") as excinfo:
        resolve_monitor_tls([_repo("a", missing)])
    assert excinfo.value.setting == "tls_cert"
    assert str(missing) in str(excinfo.value)
    assert excinfo.value.repos == ["a"]


def test_missing_key_file_refuses_naming_the_setting(tmp_path):
    cert = _make_cert(tmp_path)
    with pytest.raises(MonitorTlsError, match="tls_key") as excinfo:
        resolve_monitor_tls([_repo("a", cert, tmp_path / "nokey.pem")])
    assert excinfo.value.setting == "tls_key"


def test_unloadable_pair_refuses_never_falls_back_to_http(tmp_path):
    """A file that passes ``is_file()`` but is not a PEM must refuse here, not
    die later inside the server's background task."""
    garbage = tmp_path / "cert.pem"
    garbage.write_text("not a pem")
    with pytest.raises(MonitorTlsError, match="could not be loaded") as excinfo:
        resolve_monitor_tls([_repo("a", garbage)])
    assert excinfo.value.setting == "tls_cert"
    assert str(garbage) in str(excinfo.value)
