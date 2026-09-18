"""Tripwires for web/package.json's npm ``overrides``.

An override is a workaround with an expiry date. The lightningcss one exists
because @tailwindcss/node pins ``lightningcss`` to exactly 1.32.0, which does
not model the ``::highlight()`` pseudo-element, and Tailwind's CSS pass warned
about covapp's search-highlight rule on every build (see web/README.md,
"Dependency overrides"). These pins read the committed lockfile, so they fail
the moment a dependency bump makes the override obsolete or harmful, instead
of letting it outlive its reason unnoticed.
"""

import json
import re

import pytest

from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

_WEB = PROJECT_ROOT / "web"
_PACKAGES = json.loads((_WEB / "package-lock.json").read_text())["packages"]
_OVERRIDES = json.loads((_WEB / "package.json").read_text()).get("overrides", {})

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _parse(version: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(version)
    assert match, f"not a plain X.Y.Z version: {version!r}"
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _admits(spec: str, version: str) -> bool:
    """Whether an npm range admits *version*: exact, ``^`` and ``~`` only.

    Anything else fails loudly, so a lockfile that starts recording a range
    shape this helper doesn't model gets a reviewed update rather than a guess.
    """
    wanted = _parse(version)
    if spec.startswith("^"):
        low = _parse(spec[1:])
        if low[0] > 0:
            ceiling: tuple[int, ...] = (low[0] + 1,)
        elif low[1] > 0:
            ceiling = (0, low[1] + 1)
        else:
            ceiling = (0, 0, low[2] + 1)
        return low <= wanted < ceiling
    if spec.startswith("~"):
        low = _parse(spec[1:])
        return low <= wanted < (low[0], low[1] + 1)
    if _VERSION.fullmatch(spec):
        return _parse(spec) == wanted
    pytest.fail(f"unsupported npm range shape {spec!r}; extend _admits()")


def _recorded_dependency(package: str, dependency: str) -> str:
    entry = _PACKAGES[f"node_modules/{package}"]
    spec = entry.get("dependencies", {}).get(dependency)
    assert spec, f"{package} no longer records a {dependency} dependency"
    return spec


def _lightningcss_override() -> str:
    override = _OVERRIDES.get("@tailwindcss/node", {}).get("lightningcss")
    assert override, (
        "web/package.json no longer overrides @tailwindcss/node's lightningcss; "
        "if that was deliberate, delete this test and the README note with it"
    )
    return override


def test_tailwind_lightningcss_override_is_still_needed() -> None:
    """Fails once Tailwind itself depends on a lightningcss that knows
    ``::highlight()`` (>= 1.33): the override is then dead weight."""
    override = _lightningcss_override()
    tailwind_spec = _recorded_dependency("@tailwindcss/node", "lightningcss")
    low = _parse(tailwind_spec.lstrip("^~"))
    assert low < (1, 33, 0), (
        f"@tailwindcss/node now depends on lightningcss {tailwind_spec!r} "
        f"(>= 1.33), so the lightningcss override ({override}) is obsolete. "
        "Remove it from web/package.json, drop its note from web/README.md "
        "('Dependency overrides'), run `npm install` in web/, and delete this "
        "test."
    )


def test_vite_accepts_the_overridden_lightningcss() -> None:
    """The override is scoped to Tailwind; the tree carries one deduped
    lightningcss only while vite's own range admits the overridden version."""
    override = _lightningcss_override()
    vite_spec = _recorded_dependency("vite", "lightningcss")
    assert _admits(vite_spec, override), (
        f"vite requires lightningcss {vite_spec!r}, which excludes the "
        f"override's {override}; revisit the override in web/package.json"
    )
