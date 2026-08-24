"""Build the ``otto-fixture-beetroot`` wheel using the standard library alone.

A PEP-427 wheel is a zip holding the package plus a ``.dist-info`` directory
with METADATA, WHEEL and RECORD. Building it by hand here rather than with
``build``/``setuptools`` keeps the fixture free of a build-backend dependency,
and the result installs offline under both otto env backends (verified with
``pip install --no-index --find-links`` and ``uv pip install --no-index
--find-links``).

THE NAME IS LOAD-BEARING AND WAS WRONG ONCE. This fixture exists to be a
requirement that CANNOT be satisfied from an index, so the name must be one no
index carries. The first cut used a bare ``beetroot``, which is a REAL PyPI
project (1.1.7.4) -- `uv pip install -e tests/repo4` cheerfully fetched it, and
every test resting on "this cannot resolve" would have passed for the wrong
reason. ``otto-fixture-beetroot`` was checked against the live index and is
absent; keep any replacement equally unmistakable, and re-check it.

Regenerate with:  python tests/_fixtures/wheels/build_fixture_beetroot.py

It writes the wheel BESIDE ITSELF and says nothing on success, which is why the
``__main__`` block does not print the path: ``T201`` is exempted for
``scripts/**`` because CLI tools print by design, and widening that exemption
into ``tests/`` to announce a path the caller already knows would be the wrong
trade.
"""

import base64
import hashlib
import zipfile
from pathlib import Path

NAME = "otto_fixture_beetroot"
VERSION = "0.1.0"
DIST = f"{NAME}-{VERSION}"


def _record_line(arcname: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{arcname},sha256={digest},{len(data)}"


def build(outdir: Path) -> Path:
    """Write the wheel into *outdir* and return its path."""
    outdir.mkdir(parents=True, exist_ok=True)
    wheel = outdir / f"{DIST}-py3-none-any.whl"
    files: "dict[str, bytes]" = {
        f"{NAME}/__init__.py": (
            b'"""otto test fixture package. Never published to any index."""\n'
            b'__version__ = "0.1.0"\n\n\n'
            b"def beet() -> str:\n"
            b'    return "otto-fixture-beetroot"\n'
        ),
        f"{DIST}.dist-info/METADATA": (
            f"Metadata-Version: 2.1\nName: {NAME}\nVersion: {VERSION}\n"
            "Summary: otto test fixture package\n"
        ).encode(),
        f"{DIST}.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: otto-fixture\n"
            b"Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    records = [_record_line(name, data) for name, data in files.items()]
    records.append(f"{DIST}.dist-info/RECORD,,")
    files[f"{DIST}.dist-info/RECORD"] = ("\n".join(records) + "\n").encode()
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return wheel


if __name__ == "__main__":
    build(Path(__file__).parent)
