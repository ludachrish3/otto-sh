"""project3's coverage product, per Zephyr version: where it builds, from what, into what.

The one home of the version-to-build-dir-to-artifact mapping: the suite in
``tests/test_embedded_coverage.py`` builds and loads through it (per host, by
``os_version``), and ``scripts/bed_reap.py`` rebuilds through it whatever a cut
``west build`` left unfinished. ``product/`` is on the project's ``libs`` path,
so the suite imports this module by name; the reap puts the directory on its
path and imports it the same way.
"""

import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

import tomli

PRODUCT_DIR = Path(__file__).resolve().parent
BUILD_SCRIPT = PRODUCT_DIR / "build.sh"
SETTINGS = PRODUCT_DIR.parent / ".otto" / "settings.toml"
BUILD_TIMEOUT_S = 900
OTTO_BIN = Path(sys.executable).parent / "otto"
"""This interpreter's console script: its shipped patch and starter are what the build consumes."""


def settings() -> dict:
    """project3's settings table, read from the file (no bootstrap needed)."""
    return tomli.loads(SETTINGS.read_text())


def embedded_config(config: dict) -> dict:
    """The ``[coverage.embedded]`` table of a project's *config* (``{}`` when absent)."""
    return (config.get("coverage") or {}).get("embedded") or {}


def build_dir_for(config: dict, os_version: "str | None") -> str:
    """Resolve the ``build_dir`` for a host's Zephyr *os_version* under *config*.

    otto's own reading of the table (:func:`otto.coverage.collect.resolve_embedded_build_dir`,
    anchored to project3's root); :exc:`RuntimeError` when neither the version's
    nor the single ``build_dir`` is configured.
    """
    from otto.coverage.collect import resolve_embedded_build_dir

    build_dir = resolve_embedded_build_dir(embedded_config(config), os_version, PRODUCT_DIR.parent)
    if not build_dir:
        raise RuntimeError("[coverage.embedded].build_dir is not configured")
    return build_dir


def zver_for(os_version: "str | None") -> "str | None":
    """Map a Zephyr *os_version* to ``build.sh``'s ``zver`` positional argument.

    ``"3.7"`` → ``"v3_7"``, ``"4.4"`` → ``"v4_4"``; ``None`` for a falsy version, so
    the caller omits the argument and ``build.sh``'s default (``v3_7``) applies.
    """
    if not os_version:
        return None
    return "v" + os_version.replace(".", "_")


def extension_path(build_dir: str, extension: str) -> Path:
    """Where ``build.sh`` leaves the stripped LLEXT for *extension* under *build_dir*."""
    return Path(build_dir) / "zephyr" / f"{extension}.stripped.llext"


def build_command(build_dir: str, zver: "str | None", otto: Path) -> str:
    """The ``build.sh`` line for *build_dir* and *zver*, consuming *otto*'s shipped patch."""
    cmd = f"OTTO={shlex.quote(str(otto))} bash {BUILD_SCRIPT} {build_dir}"
    return cmd if zver is None else f"{cmd} {zver}"


async def build_extension(build_dir: str, zver: "str | None", otto: Path = OTTO_BIN) -> None:
    """Rebuild the LLEXT coverage extension into *build_dir* for Zephyr *zver*.

    Runs ``product/build.sh {build_dir} [{zver}]`` on the machine executing the
    caller (the dev VM, where *build_dir* lives) and raises if the build cannot
    run or errors. The script is idempotent, so a pre-existing build dir is fine.
    *otto* is the console script whose shipped patch and starter the build
    consumes: this interpreter's own, never whichever ``otto`` PATH finds.
    """
    from otto.host import LocalHost
    from otto.utils import Status

    localhost = LocalHost()
    try:
        result = await localhost.exec(build_command(build_dir, zver, otto), timeout=BUILD_TIMEOUT_S)
        if result.status != Status.Success:
            raise RuntimeError(f"extension build failed (see {BUILD_SCRIPT}):\n{result.value}")
    finally:
        await localhost.close()


@dataclass(frozen=True)
class ProductBuild:
    """One Zephyr version's build of the product: its dir, its ``zver`` and its artifact."""

    os_version: str
    build_dir: str
    zver: "str | None"
    artifact: Path


def declared_builds(config: "dict | None" = None) -> list[ProductBuild]:
    """Every per-version build project3 declares, in declaration order.

    One entry per ``[coverage.embedded.builds."<version>"]`` table; the artifact
    is the one declared embedded product's, under that version's build dir (the
    suite checks the same agreement per host before loading).
    """
    config = settings() if config is None else config
    names = {e["name"] for e in config.get("products", []) if e.get("kind") == "embedded"}
    if len(names) != 1:
        raise RuntimeError(f"expected one embedded product name, got {sorted(names)}")
    (name,) = names
    builds = embedded_config(config).get("builds", {})
    found = []
    for version, entry in builds.items():
        if entry.get("build_dir"):
            build_dir = build_dir_for(config, version)
            found.append(
                ProductBuild(version, build_dir, zver_for(version), extension_path(build_dir, name))
            )
    return found
