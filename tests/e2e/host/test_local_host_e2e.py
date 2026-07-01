"""End-to-end tests for ``otto host local run/put/get/login`` (no bed required).

``LocalHost`` spawns local subprocesses and copies files on the local
filesystem, so the full CLI dispatch path runs end-to-end — repo discovery,
lab loading, host resolution, command execution, file transfer — without any
network transport or remote VM.

The ``local`` host ID is a built-in host: :func:`otto.configmodule.lab.load_lab`
injects a ``LocalHost()`` into every lab it returns, on any backend, so
``otto host local`` resolves without a custom lab-repository. This fixture repo
uses the standard ``json`` backend (``[lab] backend = "json"`` in
``.otto/settings.toml``); the ``local`` host comes from the built-in injection.

Login note: ``LocalHost._interact`` is not implemented — ``otto host local
login`` exits non-zero with a clean "does not support" message (no traceback)
thanks to the ``NotImplementedError`` handler in ``otto.cli.expose``.
"""

from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import (
    REPO_E2E,
    assert_no_output_dir,
    assert_output_dir,
    run_otto,
)

pytestmark = pytest.mark.hostless


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_local_run_echo(tmp_path: Path) -> None:
    """``otto host local run "echo hello-e2e"`` exits 0 and prints the token."""
    r = run_otto(
        ["host", "local", "run", "echo hello-e2e"],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
        lab="veggies",
    )
    assert r.returncode == 0, r.stderr
    assert "hello-e2e" in r.stdout
    assert_output_dir(tmp_path, "host")  # a host verb does real work — output dir created


# ---------------------------------------------------------------------------
# put / get round-trip
# ---------------------------------------------------------------------------


def test_local_put_get_roundtrip(tmp_path: Path) -> None:
    """put copies src → dest; get copies it back; content survives both hops."""
    src = tmp_path / "payload.txt"
    src.write_text("roundtrip-token\n")

    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    # put: src file → dest_dir/payload.txt
    put = run_otto(
        ["host", "local", "put", str(src), str(dest_dir)],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
        lab="veggies",
    )
    assert put.returncode == 0, put.stderr
    dest_file = dest_dir / "payload.txt"
    assert dest_file.exists(), f"Expected {dest_file} after put"
    assert dest_file.read_text() == "roundtrip-token\n"

    # get: dest_dir/payload.txt → back_dir/payload.txt
    back_dir = tmp_path / "back"
    back_dir.mkdir()
    get = run_otto(
        ["host", "local", "get", str(dest_file), str(back_dir)],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
        lab="veggies",
    )
    assert get.returncode == 0, get.stderr
    back_file = back_dir / "payload.txt"
    assert back_file.exists(), f"Expected {back_file} after get"
    assert back_file.read_text() == "roundtrip-token\n"
    assert_output_dir(tmp_path, "host")  # put/get are host verbs — output dir created


# ---------------------------------------------------------------------------
# login — unimplemented verb exits cleanly
# ---------------------------------------------------------------------------


def test_local_login_exits_cleanly(tmp_path: Path) -> None:
    """``otto host local login`` exits non-zero with a clean error message.

    ``LocalHost`` does not override ``_interact``, so the CLI call hits the
    ``NotImplementedError`` handler in ``otto.cli.expose`` and exits 1 with a
    human-readable "does not support" message — no traceback, no crash.
    """
    r = run_otto(
        ["host", "local", "login"],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
        lab="veggies",
    )
    combined = r.stdout + r.stderr
    assert r.returncode != 0
    assert "does not support" in combined
    assert "login" in combined
    assert "Traceback (most recent call last)" not in combined
    # `login` is a host verb: the callback creates the output dir before the verb
    # runs, so the dir exists even though the (unimplemented) verb then fails.
    assert_output_dir(tmp_path, "host")


# ---------------------------------------------------------------------------
# read-only query verb — no output dir
# ---------------------------------------------------------------------------


def test_local_exists_creates_no_output_dir(tmp_path: Path) -> None:
    """`otto host local exists <path>` is a read-only query (output_dir=False) — no dir."""
    r = run_otto(
        ["host", "local", "exists", str(tmp_path)],
        xdir=tmp_path,
        sut_dirs=REPO_E2E,
        lab="veggies",
    )
    assert r.returncode == 0, r.stderr
    assert_no_output_dir(tmp_path)
