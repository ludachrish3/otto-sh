"""The staged env launcher: the one way a use-case's mapping reaches compose.

Every compose call of a deployment runs as ``sh <launcher> docker compose …``.
These tests run the rendered launcher through the real ``/bin/sh`` with a
hostile mapping and a parent environment that disagrees with it, so what they
pin is what a shell on the parent does with the text, not what the text looks
like.
"""

import json
import subprocess
import sys

import pytest

from otto.docker import deployment as deploy_mod
from otto.docker.compose import compose_command
from otto.docker.resolve import UseCaseResolutionError
from otto.host.command_frame import BashFrame, SessionMarkers

_HOSTILE = {
    "DOLLAR": "${OTHER}",
    "REPEATED": "$$x$",
    "HASH": "a # b",
    "HASH_TIGHT": "a#b",
    "QUOTES": "'single' \"double\"",
    "BACKSLASHES": "a\\b\\",
    "BACKSLASH_QUOTE": "\\'",
    "BACKSLASH_DOLLAR": "\\$x",
    "SPACES": "  padded  ",
    "EMPTY": "",
    "NEWLINE": "line1\nline2",
    "LITERAL_BACKSLASH_N": "a\\nb",
    "BACKTICK": "`id`",
    "a.b": "dotted key",
    "IMAGE": "alpine:3",
}

_PRINT_ENV = [sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"]


def _run(tmp_path, mapping, argv, parent_env):
    launcher = tmp_path / "otto-env.sh"
    launcher.write_text(deploy_mod._launcher_text(mapping))
    return subprocess.run(
        ["/bin/sh", str(launcher), *argv],
        env=parent_env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_every_value_reaches_the_command_as_written_and_beats_the_parent(tmp_path):
    parent_env = {"PATH": "/usr/bin:/bin", "IMAGE": "", "HASH": "from the parent", "KEPT": "k"}

    ran = _run(tmp_path, _HOSTILE, _PRINT_ENV, parent_env)

    assert ran.returncode == 0, ran.stderr
    seen = json.loads(ran.stdout)
    assert {k: seen.get(k) for k in _HOSTILE} == _HOSTILE
    assert seen["KEPT"] == "k", "a parent variable the mapping does not name is inherited"


def test_the_command_line_after_the_launcher_reaches_the_command_untouched(tmp_path):
    argv = [sys.executable, "-c", "import json, sys; print(json.dumps(sys.argv[1:]))"]
    words = ["a b", "$HOME", "'q'", "*", "", "--flag"]

    ran = _run(tmp_path, {"K": "v"}, [*argv, *words], {"PATH": "/usr/bin:/bin"})

    assert json.loads(ran.stdout) == words


def test_the_command_exit_status_is_the_launcher_exit_status(tmp_path):
    ran = _run(
        tmp_path, {}, [sys.executable, "-c", "raise SystemExit(7)"], {"PATH": "/usr/bin:/bin"}
    )

    assert ran.returncode == 7


def test_an_empty_mapping_still_runs_the_command(tmp_path):
    ran = _run(tmp_path, {}, _PRINT_ENV, {"PATH": "/usr/bin:/bin", "KEPT": "k"})

    assert json.loads(ran.stdout)["KEPT"] == "k"


def test_the_launcher_text_is_stable_whatever_the_mapping_order():
    assert deploy_mod._launcher_text({"B": "2", "A": "1"}) == deploy_mod._launcher_text(
        {"A": "1", "B": "2"}
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("", "v"),
        ("K=V", "v"),
        ("-i", "v"),
        ("K\0", "v"),
        ("K", "v\0"),
    ],
)
def test_a_key_or_value_env_cannot_carry_is_refused(key, value):
    with pytest.raises(UseCaseResolutionError, match="cannot be passed"):
        deploy_mod._launcher_text({key: value})


def test_a_pooled_shell_keeps_nothing_from_a_command_run_under_the_launcher(tmp_path):
    """Telnet and console hosts run every command in one long-lived shell.

    Two framed commands go to one real shell, the way such a session sends
    them: the first runs compose's command shape under the launcher (with
    ``env`` standing in for ``docker``), the second asks for the variable.
    The launcher's ``exec`` must replace only its own child ``sh``, so the
    session reaches the second END marker, and the value must not outlive it.
    """
    launcher = tmp_path / "otto-env.sh"
    launcher.write_text(deploy_mod._launcher_text({"OTTO_LEAK_PROBE": "set"}))
    under_launcher = compose_command("p", launcher=launcher).replace(" docker compose -p p", " env")
    markers = SessionMarkers.for_session("cafef00d")
    frame = BashFrame()
    session_input = frame.frame(under_launcher, markers) + frame.frame(
        'echo "probe=${OTTO_LEAK_PROBE-unset}"', markers
    )

    ran = subprocess.run(
        ["/bin/sh"],
        input=session_input,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )

    first, second = ran.stdout.split(f"{markers.end_prefix}0__")[:2]
    assert "OTTO_LEAK_PROBE=set" in first
    assert "probe=unset" in second
    assert ran.stdout.count(f"{markers.end_prefix}0__") == 2, ran.stdout
