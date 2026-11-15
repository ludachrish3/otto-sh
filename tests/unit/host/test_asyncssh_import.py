"""otto's import of asyncssh never starts a compiler to look for a library.

``import asyncssh`` probes for liboqs and nettle with
``ctypes.util.find_library`` while it loads. On glibc Linux that function reads
the dynamic linker's cache first (``ldconfig -p``) and, for a name the cache
does not list, runs ``gcc`` and then ``ld`` to ask them instead. A machine
without liboqs therefore paid two compiler runs and two linker runs on every
command that reached SSH (#659).

:mod:`otto.host._asyncssh` is the one place otto imports asyncssh, and it does
so with the lookup held to the linker cache.

These tests run in a clean subprocess because ``sys.modules`` is
process-global: asyncssh loaded by any earlier test would make the import under
test a no-op. Each one replaces the three legs of the stdlib lookup with
recorders that find nothing, which is the worst case (every name a miss) and
needs no library, compiler or ``ldconfig`` on the machine running it.
"""

import json
import subprocess
import sys

from tests.e2e._otto_subprocess import CHECKOUT_ROOT, coverage_subprocess_env

PROBED_LIBRARIES = ["libnettle", "libnettle-6", "liboqs", "nettle", "oqs"]
"""Every name asyncssh asks ``find_library`` for when none of them resolves.

Pinned so that an asyncssh release probing for something new is seen here: a
new name is a new library whose discovery otto now holds to the linker cache.
"""

_RECORD_LEGS = """
import ctypes.util, json, sys

asked = {"cache": [], "gcc": [], "ld": []}
spawned = []

def recorder(leg):
    def find_nothing(name):
        asked[leg].append(name)
        return None
    return find_nothing

ctypes.util._findSoname_ldconfig = recorder("cache")
ctypes.util._findLib_gcc = recorder("gcc")
ctypes.util._findLib_ld = recorder("ld")

def audit(event, args):
    if event == "subprocess.Popen":
        spawned.append(str(args[0]))

sys.addaudithook(audit)
platform_lookup = ctypes.util.find_library
"""

_REPORT = """
print(json.dumps({
    "asked": {leg: sorted(names) for leg, names in asked.items()},
    "spawned": spawned,
    "restored": ctypes.util.find_library is platform_lookup,
    "loaded": "asyncssh" in sys.modules,
}))
"""


def _run(script: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=coverage_subprocess_env(),
        capture_output=True,
        text=True,
        cwd=str(CHECKOUT_ROOT),
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"probe failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_connect_path_asks_only_the_linker_cache() -> None:
    """Importing what an SSH connection imports reaches neither gcc nor ld."""
    seen = _run(_RECORD_LEGS + "import otto.host.host_key_client\n" + _REPORT)

    assert seen["loaded"] is True
    assert seen["asked"]["gcc"] == []
    assert seen["asked"]["ld"] == []
    assert seen["spawned"] == []


def test_the_probed_names_are_the_pinned_set() -> None:
    """asyncssh asks for the pinned library names and no others."""
    seen = _run(_RECORD_LEGS + "import otto.host._asyncssh\n" + _REPORT)

    assert seen["asked"]["cache"] == PROBED_LIBRARIES


def test_the_platform_lookup_is_put_back() -> None:
    """Once asyncssh is loaded, ``ctypes.util.find_library`` is the stdlib's again."""
    seen = _run(_RECORD_LEGS + "import otto.host._asyncssh\n" + _REPORT)

    assert seen["restored"] is True


def test_a_platform_without_the_cache_leg_keeps_its_own_lookup() -> None:
    """Where the stdlib has no linker-cache leg, asyncssh loads through ``find_library`` as is.

    Only the glibc branch of ``ctypes.util`` defines the leg; macOS and the
    BSDs resolve a library without starting a compiler, so there is nothing to
    hold back there.
    """
    script = (
        _RECORD_LEGS
        + "del ctypes.util._findSoname_ldconfig\n"
        + "ctypes.util.find_library = platform_lookup = recorder('platform')\n"
        + "asked['platform'] = []\n"
        + "import otto.host._asyncssh\n"
        + _REPORT
    )
    seen = _run(script)

    assert seen["loaded"] is True
    assert seen["asked"]["platform"] == PROBED_LIBRARIES
    assert seen["restored"] is True


def test_the_platform_lookup_is_put_back_when_the_import_fails() -> None:
    """An asyncssh import that raises still leaves ``find_library`` the stdlib's."""
    script = (
        _RECORD_LEGS
        + "def refuse(name):\n"
        + "    raise RuntimeError('no lookup')\n"
        + "ctypes.util._findSoname_ldconfig = refuse\n"
        + "try:\n"
        + "    import otto.host._asyncssh\n"
        + "except RuntimeError:\n"
        + "    pass\n"
        + _REPORT
    )
    seen = _run(script)

    assert seen["loaded"] is False
    assert seen["restored"] is True
