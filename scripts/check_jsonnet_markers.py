"""Check which Jsonnet binding ``pyproject.toml``'s markers install, per target and Python.

otto declares two bindings, each behind a platform marker: the C++ ``jsonnet``
and the Go ``gojsonnet`` (docs/installation.md, "Known gaps"). A marker sees
the platform, the machine and the Python, never the libc, so whether a target
gets a usable binding is a question only a resolver answers. :func:`resolve`
asks ``uv pip compile`` for one target and one Python, binary wheels only, and
returns the binding the resolution holds. :func:`audit` checks every case the
tables below pin: :data:`EXPECTED` for each supported minor,
:data:`EXPECTED_315` on the next-Python canary (nightly's ``CANARY_PYTHON``,
3.15 today), and the two documented gaps in :data:`NO_WHEEL`,
whose binary-only resolve must fail naming the binding the marker selects.

The real resolutions reach the package index (PyPI), so they run here, in
``make check-jsonnet-markers`` and CI's ``lint-python`` job, never in a test
suite: ``tests/unit/test_jsonnet_markers.py`` drives the same logic through a
fake runner.

Usage: python -m scripts.check_jsonnet_markers
"""

import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scripts.check_docs_wheel_matrix import supported_minors

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
NIGHTLY = ROOT / ".github" / "workflows" / "nightly.yml"

#: The distributions that carry a Jsonnet binding, by their requirement name.
BINDINGS = ("jsonnet", "gojsonnet")

#: The binding each uv ``--python-platform`` target resolves to, for every
#: supported minor.
EXPECTED: "dict[str, str]" = {
    "x86_64-manylinux_2_28": "jsonnet",
    "x86_64-unknown-linux-musl": "jsonnet",
    "aarch64-manylinux2014": "gojsonnet",
    "aarch64-apple-darwin": "jsonnet",
    "x86_64-pc-windows-msvc": "jsonnet",
}
#: The same on the next-Python canary (:func:`canary_python`, 3.15 today), which
#: otto does not declare yet: the C++ wheel is abi3, and gojsonnet publishes no
#: cp315 wheel, so its marker stops at 3.14 (``None``: neither binding, as on
#: Linux aarch64 here).
EXPECTED_315: "dict[str, str | None]" = {
    "x86_64-manylinux_2_28": "jsonnet",
    "aarch64-manylinux2014": None,
}
#: The documented gaps: the marker selects a binding with no wheel for the
#: target, so a binary-only resolve fails naming it, for every supported minor.
NO_WHEEL: "dict[str, str]" = {
    "x86_64-manylinux2014": "jsonnet",  # glibc < 2.28: the marker selects jsonnet, no wheel fits
    "aarch64-unknown-linux-musl": "gojsonnet",  # musl aarch64: gojsonnet, no musllinux wheel
}


@dataclass(frozen=True)
class Compiled:
    """What one ``uv pip compile`` run returned."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str]], Compiled]
"""Run a ``uv pip compile`` argv and return what it printed; :func:`uv_compile` by default."""


@dataclass(frozen=True)
class Case:
    """One pinned resolution: *binding* on *target* under Python *minor*, or a failure naming it."""

    target: str
    minor: str
    binding: "str | None"
    fails: bool = False


class ResolveFailedError(Exception):
    """The binary-only resolution failed; :attr:`stderr` is uv's own explanation."""

    def __init__(self, target: str, minor: str, stderr: str) -> None:
        super().__init__(
            f"no binary-only resolution for {target} on Python {minor}:\n{stderr.strip()}"
        )
        self.target = target
        self.minor = minor
        self.stderr = stderr


def canary_python(nightly_text: str) -> str:
    """Return the next-Python canary nightly climbs: ``python-canary``'s ``CANARY_PYTHON``."""
    import yaml

    value = yaml.safe_load(nightly_text)["jobs"]["python-canary"]["env"]["CANARY_PYTHON"]
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+", value):
        msg = f"nightly's CANARY_PYTHON must be a quoted MAJOR.MINOR string, not {value!r}"
        raise ValueError(msg)
    return value


def uv_compile(argv: list[str]) -> Compiled:
    """Run *argv* (a ``pip compile`` invocation, uv itself excluded) with otto's uv."""
    from otto.env.backends import uv_bin

    proc = subprocess.run(  # noqa: S603 -- fixed argv, uv from the venv
        [str(uv_bin()), *argv], capture_output=True, text=True, check=False
    )
    return Compiled(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def compile_argv(target: str, minor: str, pyproject: Path) -> list[str]:
    """Spell the binary-only ``uv pip compile`` argv for *target* under Python *minor*."""
    return [
        "pip",
        "compile",
        str(pyproject),
        "--python-platform",
        target,
        "--python-version",
        minor,
        "--only-binary",
        ":all:",
        "--no-header",
        "--no-annotate",
        "--quiet",
    ]


def resolve(
    target: str, minor: str, *, pyproject: Path = PYPROJECT, run: Runner = uv_compile
) -> "str | None":
    """Return the binding *pyproject* installs on *target* under Python *minor*, or ``None``.

    *target* is a uv ``--python-platform`` value (``x86_64-manylinux_2_28``,
    ``aarch64-apple-darwin``, ...). Only binary wheels count
    (``--only-binary :all:``), as an air-gapped install has nothing to build
    with. Raises :class:`ResolveFailedError` when no binary-only resolution
    exists.
    """
    compiled = run(compile_argv(target, minor, pyproject))
    if compiled.returncode != 0:
        raise ResolveFailedError(target, minor, compiled.stderr)
    found = [
        name for name in BINDINGS if re.search(rf"^{name}==", compiled.stdout, flags=re.MULTILINE)
    ]
    if len(found) > 1:
        msg = f"{target} on Python {minor} resolves both bindings: {found}"
        raise ValueError(msg)
    return found[0] if found else None


def names_only(stderr: str, binding: str) -> bool:
    """Whether *stderr* names *binding* and not the other binding.

    Word-bounded, so ``jsonnet`` never matches inside ``gojsonnet``.
    """
    (other,) = set(BINDINGS) - {binding}
    return bool(re.search(rf"\b{binding}\b", stderr)) and not re.search(rf"\b{other}\b", stderr)


def cases(minors: list[str], next_minor: str) -> list[Case]:
    """Every pinned resolution for the supported *minors* and the canary *next_minor*."""
    return [
        *(Case(target, minor, binding) for target, binding in EXPECTED.items() for minor in minors),
        *(Case(target, next_minor, binding) for target, binding in EXPECTED_315.items()),
        *(
            Case(target, minor, binding, fails=True)
            for target, binding in NO_WHEEL.items()
            for minor in minors
        ),
    ]


def check(case: Case, *, pyproject: Path = PYPROJECT, run: Runner = uv_compile) -> "str | None":
    """Return what is wrong with *case* on the real resolution, or ``None`` when it holds."""
    where = f"{case.target} on Python {case.minor}"
    try:
        found = resolve(case.target, case.minor, pyproject=pyproject, run=run)
    except ResolveFailedError as exc:
        if not case.fails:
            return f"{where}: expected {case.binding or 'no binding'}, but {exc}"
        if case.binding is not None and not names_only(exc.stderr, case.binding):
            return (
                f"{where}: the binary-only resolve failed without naming "
                f"{case.binding} alone:\n{exc.stderr.strip()}"
            )
        return None
    if case.fails:
        return (
            f"{where}: expected the binary-only resolve to fail naming {case.binding}, "
            f"but it resolved {found or 'no binding'}"
        )
    if found != case.binding:
        return f"{where}: expected {case.binding or 'no binding'}, resolved {found or 'no binding'}"
    return None


def audit(
    minors: list[str], next_minor: str, *, pyproject: Path = PYPROJECT, run: Runner = uv_compile
) -> list[str]:
    """Return every pinned case the resolution contradicts; empty when all of them hold."""
    return [
        problem
        for case in cases(minors, next_minor)
        if (problem := check(case, pyproject=pyproject, run=run)) is not None
    ]


def main(argv: list[str]) -> int:
    """Resolve every pinned case against PyPI; print each contradiction and return 1 on any."""
    if argv:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    minors = [f"{major}.{minor}" for major, minor in supported_minors(PYPROJECT.read_text())]
    if not minors:
        print(f"no Python minor classifiers in {PYPROJECT}", file=sys.stderr)
        return 1
    next_minor = canary_python(NIGHTLY.read_text())
    if next_minor in minors:
        print(f"{next_minor} is now supported: fold EXPECTED_315 into EXPECTED", file=sys.stderr)
        return 1
    problems = audit(minors, next_minor)
    for problem in problems:
        print(problem)
    if not problems:
        print(f"jsonnet markers: OK ({len(cases(minors, next_minor))} resolutions)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
