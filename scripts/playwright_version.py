"""Name the Playwright image the browser-test CI jobs run in, from uv.lock.

This is the one place that reads the locked ``playwright`` version and the one
place that spells the image name. CI's ``playwright-version`` job runs it and
hands ``version`` and ``image`` to the browser jobs as job outputs, so a
Dependabot bump of the lock moves the image with it and no workflow carries a
hand-pinned tag.

Run it as ``uv run --no-project --with tomli python scripts/playwright_version.py uv.lock``:
that installs ``tomli`` alone, not the project.
"""

import os
import re
import sys
from pathlib import Path

import tomli

IMAGE_REPOSITORY = "mcr.microsoft.com/playwright/python"
IMAGE_SUFFIX = "noble"

_NUMERIC_VERSION = re.compile(r"\d+\.\d+\.\d+")


def locked_version(lock_path: Path) -> str:
    """Return the single ``playwright`` version in ``lock_path``, or exit saying why not."""
    packages = tomli.loads(lock_path.read_text())["package"]
    versions = sorted({p["version"] for p in packages if p["name"] == "playwright"})
    if not versions:
        raise SystemExit(f"{lock_path}: no playwright package is locked")
    if len(versions) > 1:
        raise SystemExit(f"{lock_path}: more than one playwright version is locked: {versions}")
    if not _NUMERIC_VERSION.fullmatch(versions[0]):
        raise SystemExit(f"{lock_path}: playwright version {versions[0]!r} is not N.N.N")
    return versions[0]


def image_for(version: str) -> str:
    """Return the Playwright Python image for ``version``."""
    return f"{IMAGE_REPOSITORY}:v{version}-{IMAGE_SUFFIX}"


def main(argv: list[str] | None = None) -> int:
    """Print the image for the lock named in ``argv`` (default ``uv.lock``) and export it to CI."""
    args = sys.argv[1:] if argv is None else argv
    version = locked_version(Path(args[0] if args else "uv.lock"))
    image = image_for(version)
    print(image)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a") as fh:
            fh.write(f"version={version}\nimage={image}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
