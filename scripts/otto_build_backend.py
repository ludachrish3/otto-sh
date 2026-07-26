"""In-tree PEP 517 backend: uv_build, plus a refuse-to-ship-asset-less guard.

otto labs are air-gapped, so the two web frontends (the monitor dashboard and
the covapp coverage SPA) must travel INSIDE the wheel — ``uv_build`` embeds
whatever is on disk under ``src/otto/``, which means a bare ``uv build`` on a
checkout that never ran ``make web`` silently produces a wheel that installs
fine and then fails at first use in an environment that cannot rebuild the
assets. This shim (wired via ``[build-system] backend-path`` in pyproject)
turns that silent hole into a build-time error for every frontend — ``uv
build``, ``pip wheel``, ``python -m build``, the tag-triggered release
workflows — not just the ``make`` flow that ``wheel-check`` already gates.

Guarded: ``build_wheel`` and ``build_sdist`` (a source distribution without
the assets could never yield a valid wheel either — ``uv build`` builds the
wheel FROM the sdist). Deliberately unguarded: ``build_editable`` and every
metadata/requires hook, so ``uv sync`` on a fresh checkout (dev boxes,
hostless CI, Read the Docs) keeps working before any ``make web`` — editable
installs resolve the assets from the live tree, which self-heals once built.

``import uv_build`` happens lazily inside each hook: the dev venv does not
install uv_build (it exists only in PEP 517 isolated build environments), and
the unit-lane guard test imports this module directly to pin its policy.

The required-file list must stay in step with ``otto._webassets.ALL`` and the
Makefile ``wheel-check`` loop; ``tests/unit/test_webassets_guard.py`` pins the
coupling.
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# One entry per registered web artifact (otto._webassets.ALL): the file whose
# absence proves the bundle was never built. Mirrors the Makefile wheel-check
# loop ("monitor/dist/index.html covapp/index.html").
REQUIRED_WEBASSETS = (
    "src/otto/_webassets/monitor/dist/index.html",
    "src/otto/_webassets/covapp/index.html",
)


def assert_webassets_present(kind: str, root: Path = _ROOT) -> None:
    """Raise if any registered web artifact is missing from *root*."""
    missing = [rel for rel in REQUIRED_WEBASSETS if not (root / rel).is_file()]
    if missing:
        raise RuntimeError(
            f"refusing to build an asset-less {kind}: missing {', '.join(missing)}. "
            "Run `make web` first — otto labs are air-gapped and install the web "
            "frontends from the wheel, so a distribution built without them fails "
            "at first use where it cannot be rebuilt."
        )


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):  # noqa: ANN001, ANN201
    """Build the wheel via uv_build — after refusing if the web assets are absent."""
    # Assert BEFORE importing uv_build: under --no-build-isolation uv_build may
    # be missing from the ambient env, and the actionable refuse-message should
    # win over a ModuleNotFoundError.
    assert_webassets_present("wheel")
    import uv_build

    return uv_build.build_wheel(wheel_directory, config_settings, metadata_directory)


def build_sdist(sdist_directory, config_settings=None):  # noqa: ANN001, ANN201
    """Build the sdist via uv_build — after refusing if the web assets are absent."""
    assert_webassets_present("sdist")  # before the import; same rationale as build_wheel
    import uv_build

    return uv_build.build_sdist(sdist_directory, config_settings)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):  # noqa: ANN001, ANN201
    """Delegate to uv_build unguarded: editable installs resolve assets from the live tree."""
    import uv_build

    return uv_build.build_editable(wheel_directory, config_settings, metadata_directory)


def get_requires_for_build_wheel(config_settings=None):  # noqa: ANN001, ANN201
    """Delegate to uv_build (PEP 517 optional hook)."""
    import uv_build

    return uv_build.get_requires_for_build_wheel(config_settings)


def get_requires_for_build_sdist(config_settings=None):  # noqa: ANN001, ANN201
    """Delegate to uv_build (PEP 517 optional hook)."""
    import uv_build

    return uv_build.get_requires_for_build_sdist(config_settings)


def get_requires_for_build_editable(config_settings=None):  # noqa: ANN001, ANN201
    """Delegate to uv_build (PEP 517 optional hook)."""
    import uv_build

    return uv_build.get_requires_for_build_editable(config_settings)


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):  # noqa: ANN001, ANN201
    """Delegate to uv_build (PEP 517 optional hook)."""
    import uv_build

    return uv_build.prepare_metadata_for_build_wheel(metadata_directory, config_settings)


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):  # noqa: ANN001, ANN201
    """Delegate to uv_build (PEP 517 optional hook)."""
    import uv_build

    return uv_build.prepare_metadata_for_build_editable(metadata_directory, config_settings)
