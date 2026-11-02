"""The uv otto runs: where it is, what version it is, and the argv otto hands it.

Every exec goes through the ``exec_seam`` fake (``conftest.py``), so no test
here runs a real installer.
"""

import sys
import types
from pathlib import Path

import pytest

from otto.env.backends import (
    UvNotFoundError,
    create_venv,
    install,
    uv_bin,
    uv_version,
    venv_python,
)

_UV = Path("/o/uv")


class TestInstallerArgv:
    """Otto's whole contribution to an installer run: the argv it hands over.

    The e2e contrast in ``tests/e2e/cli/test_env_e2e.py`` proves a REAL
    resolver honours the tokens after ``--``, and pays a real venv plus a real
    editable install for that proof. Whether the tokens reach uv's argv is
    otto's code, not uv's, and that is what these pin: no venv, no index, no
    network, milliseconds, so the assertion can never turn into a measurement
    of the machine (#402).
    """

    def test_install_runs_the_given_uv_with_the_passthrough_verbatim_and_last(
        self, exec_seam, tmp_path
    ):
        """Last is the contract: the operator's tokens must be able to override otto's."""
        env = tmp_path / "env"
        result = install(_UV, env, ["-e", "/project4"], ["--find-links", "/wheels"])

        assert len(exec_seam) == 1, exec_seam
        argv = exec_seam[0]
        assert argv == [
            "/o/uv",
            "pip",
            "install",
            "--python",
            str(venv_python(env)),
            "-e",
            "/project4",
            "--find-links",
            "/wheels",
        ]
        assert result.args == argv

    def test_create_venv_runs_the_given_uv(self, exec_seam, tmp_path):
        env = tmp_path / "env"
        create_venv(_UV, env)

        assert exec_seam == [["/o/uv", "venv", str(env)]]

    def test_no_passthrough_adds_nothing(self, exec_seam, tmp_path):
        """An empty passthrough must not leave a stray token in the argv."""
        install(_UV, tmp_path / "env", ["-e", "/project4"], [])

        assert len(exec_seam) == 1, exec_seam
        assert exec_seam[0][-2:] == ["-e", "/project4"]


_LOCATE_MESSAGE = "Reinstall otto with its dependencies"


class TestLocateUv:
    def test_uv_bin_returns_the_located_binary(self, monkeypatch):
        monkeypatch.setattr("uv.find_uv_bin", lambda: "/x/uv")
        assert uv_bin() == Path("/x/uv")

    def test_uv_bin_package_without_binary_raises(self, monkeypatch):
        def _missing():
            raise FileNotFoundError("no uv")

        monkeypatch.setattr("uv.find_uv_bin", _missing)
        with pytest.raises(UvNotFoundError, match=_LOCATE_MESSAGE) as exc:
            uv_bin()
        assert isinstance(exc.value.__cause__, FileNotFoundError)

    def test_the_remedy_installs_only_what_is_missing(self, monkeypatch):
        """``pip install otto-sh`` on an installed otto adds the missing uv alone;
        ``--force-reinstall`` would re-download otto's whole closure."""
        monkeypatch.setitem(sys.modules, "uv", None)
        with pytest.raises(UvNotFoundError) as exc:
            uv_bin()
        assert "`pip install otto-sh`" in str(exc.value)
        assert "--force-reinstall" not in str(exc.value)

    def test_uv_bin_package_absent_raises(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "uv", None)
        with pytest.raises(UvNotFoundError, match=_LOCATE_MESSAGE):
            uv_bin()

    @pytest.mark.parametrize(
        "sentinel",
        [ImportError("boom"), ModuleNotFoundError("x", name="some_uv_dependency")],
    )
    def test_uv_bin_unrelated_import_error_propagates(self, monkeypatch, sentinel):
        fake = types.ModuleType("uv")

        def _getattr(name):
            raise sentinel

        fake.__getattr__ = _getattr
        monkeypatch.setitem(sys.modules, "uv", fake)
        with pytest.raises(type(sentinel)) as exc:
            uv_bin()
        assert exc.value is sentinel


class TestUvVersion:
    def test_uv_version_parses_the_second_token(self, exec_seam):
        exec_seam.stdout = "uv 0.13.6 (1a2b3c 2026-10-01)\n"
        path = Path("/x/uv")
        assert uv_version(path) == "0.13.6"
        assert exec_seam == [[str(path), "--version"]]

    def test_uv_version_nonzero_exit_raises(self, exec_seam):
        exec_seam.stdout = "uv 0.13.6\n"
        exec_seam.returncode = 2
        with pytest.raises(UvNotFoundError, match=r"/x/uv --version` exited 2"):
            uv_version(Path("/x/uv"))

    @pytest.mark.parametrize("stdout", ["", "foo 1.2", "uv", "uv garbage"])
    def test_uv_version_unparseable_output_raises(self, exec_seam, stdout):
        exec_seam.stdout = stdout
        with pytest.raises(UvNotFoundError, match="/x/uv"):
            uv_version(Path("/x/uv"))

    @pytest.mark.parametrize("error", [FileNotFoundError("nope"), PermissionError("denied")])
    def test_uv_version_unrunnable_executable_raises(self, exec_seam, error):
        exec_seam.raises = error
        with pytest.raises(UvNotFoundError, match="/x/uv"):
            uv_version(Path("/x/uv"))
