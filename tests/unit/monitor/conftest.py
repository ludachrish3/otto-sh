"""Shared fixtures for tests/unit/monitor."""

import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_static_dir(hermetic_monitor_dist: object) -> None:
    """Give every test in this package a throwaway dist build.

    ``MonitorServer.__init__`` -> ``_build_app`` -> ``_dist_index_path()``
    hard-requires a real ``dist/index.html`` under ``_STATIC_DIR`` (see
    ``otto.monitor.server``) — a deliberate fail-fast for real deployments
    that skipped ``make web``. Most tests in this package construct a
    ``MonitorServer`` to exercise something unrelated (port binding, the
    delete-event route, display-host resolution, ...) and have no interest in
    the dashboard frontend itself, so they shouldn't need a real React build
    in the checkout to pass. The work lives in the root conftest's
    ``hermetic_monitor_dist``, which server-booting tests elsewhere request by
    name; this fixture only makes it automatic package-wide.

    Tests that specifically exercise the *missing*-dist error path (e.g.
    ``TestDashboardRoute.test_raises_when_dist_absent``) call
    ``monkeypatch.setattr(server_module, "_STATIC_DIR", ...)`` again inside
    the test body — that later call on the same ``monkeypatch`` fixture
    simply overrides this one.
    """


@pytest.fixture
def tls_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Mint a throwaway self-signed certificate and key as two PEM files: ``(cert, key)``.

    Two files, not a bundle, so a test can tell the cert from the key: a
    wiring that swapped them would still find both paths present.
    """
    cert, key = tmp_path / "tls-cert.pem", tmp_path / "tls-key.pem"
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
            str(cert),
            "-subj",
            "/CN=127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key
