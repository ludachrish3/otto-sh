"""Backend selection: flag > settings > recorded > auto-detect, and the argv each builds.

The order is the contract. Every arm is tested from BOTH directions -- the
value it picks and the value it declines to pick -- so no arm can pass by
always returning the same answer.
"""

import subprocess
from types import SimpleNamespace

import pytest

from otto.env import backends
from otto.env.backends import BackendUnavailableError, install, select_backend, venv_python


class TestOverrideOrder:
    def test_flag_beats_everything(self, monkeypatch):
        monkeypatch.setattr("otto.env.backends._uv_on_path", lambda: True)
        assert select_backend("pip", "uv", "uv") == "pip"
        assert select_backend("uv", "pip", "pip") == "uv"

    def test_settings_beats_recorded_and_autodetect(self, monkeypatch):
        monkeypatch.setattr("otto.env.backends._uv_on_path", lambda: True)
        assert select_backend(None, "pip", "uv") == "pip"

    def test_recorded_beats_autodetect(self, monkeypatch):
        """An existing env keeps its backend; switching is a create --force matter."""
        monkeypatch.setattr("otto.env.backends._uv_on_path", lambda: True)
        assert select_backend(None, None, "pip") == "pip"

    def test_autodetect_prefers_uv_when_present(self, monkeypatch):
        monkeypatch.setattr("otto.env.backends._uv_on_path", lambda: True)
        assert select_backend(None, None, None) == "uv"

    def test_autodetect_falls_back_to_pip(self, monkeypatch):
        monkeypatch.setattr("otto.env.backends._uv_on_path", lambda: False)
        assert select_backend(None, None, None) == "pip"


class TestRefusals:
    def test_an_unknown_name_is_refused_naming_both_valid_ones(self):
        with pytest.raises(BackendUnavailableError) as exc:
            select_backend("conda", None, None)
        assert "conda" in str(exc.value)
        assert "uv" in str(exc.value)
        assert "pip" in str(exc.value)

    def test_asking_for_uv_without_uv_is_refused_not_silently_downgraded(self, monkeypatch):
        """The whole point of an explicit flag is that it does not get ignored."""
        monkeypatch.setattr("otto.env.backends._uv_on_path", lambda: False)
        with pytest.raises(BackendUnavailableError) as exc:
            select_backend("uv", None, None)
        assert "not on PATH" in str(exc.value)

    def test_pip_is_always_available(self, monkeypatch):
        monkeypatch.setattr("otto.env.backends._uv_on_path", lambda: False)
        assert select_backend("pip", None, None) == "pip"


@pytest.fixture
def exec_seam(monkeypatch):
    """Record the installer argv at the seam the exec lives at, and seal every other.

    ``backends._run`` is left alone deliberately: the thing worth pinning is
    the argv that reaches ``subprocess.run``, so the fake sits there. Every
    other exec entry point reachable from that module is replaced by a raiser,
    so a probe that ever escapes the fake fails loudly on EVERY machine instead
    of quietly running a real installer on the one that has an index.

    THE WHOLE MODULE REFERENCE IS SWAPPED, not an attribute on it:
    ``backends.subprocess`` IS the stdlib module object, so setting ``run`` on
    it would replace the real ``subprocess.run`` process-wide for the duration
    of the test. Binding a stand-in namespace to the name ``backends`` looks up
    confines the fake to the module under test.
    """
    calls: "list[list[str]]" = []

    def _record(argv, *args, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def _escaped(*args, **kwargs):
        raise AssertionError(f"a real subprocess escaped the fake: {args!r}")

    monkeypatch.setattr(
        backends,
        "subprocess",
        SimpleNamespace(
            run=_record,
            Popen=_escaped,
            CompletedProcess=subprocess.CompletedProcess,
        ),
    )
    return calls


class TestInstallerArgv:
    """Otto's whole contribution to an installer run: the argv it hands over.

    The e2e contrast in ``tests/e2e/cli/test_env_e2e.py`` proves a REAL
    resolver honours the tokens after ``--``, and pays a real venv plus a real
    editable install for that proof -- so it buys it once, on the cheap
    backend. Whether the tokens reach pip's argv is otto's code, not pip's, and
    that is what these pin: no venv, no index, no network, milliseconds, so the
    assertion can never turn into a measurement of the machine (#402).
    """

    def test_pip_appends_the_passthrough_verbatim_and_last(self, exec_seam, tmp_path):
        """Last is the contract: the operator's tokens must be able to override otto's."""
        env = tmp_path / "env"
        result = install("pip", env, ["-e", "/repo4"], ["--find-links", "/wheels"])

        assert len(exec_seam) == 1, exec_seam
        argv = exec_seam[0]
        assert argv == [
            str(venv_python(env)),
            "-m",
            "pip",
            "install",
            "-e",
            "/repo4",
            "--find-links",
            "/wheels",
        ]
        assert result.args == argv

    def test_uv_appends_the_passthrough_verbatim_and_last(self, exec_seam, tmp_path):
        env = tmp_path / "env"
        install("uv", env, ["-e", "/repo4"], ["--find-links", "/wheels"])

        assert len(exec_seam) == 1, exec_seam
        assert exec_seam[0] == [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv_python(env)),
            "-e",
            "/repo4",
            "--find-links",
            "/wheels",
        ]

    @pytest.mark.parametrize("backend", ["uv", "pip"])
    def test_no_passthrough_adds_nothing(self, exec_seam, tmp_path, backend):
        """An empty passthrough must not leave a stray token in the argv."""
        install(backend, tmp_path / "env", ["-e", "/repo4"], [])

        assert len(exec_seam) == 1, exec_seam
        assert exec_seam[0][-2:] == ["-e", "/repo4"]
