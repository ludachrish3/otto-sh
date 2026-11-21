"""The Jsonnet binding marker check, offline: ``scripts/check_jsonnet_markers.py``.

Every test drives the check through a fake ``uv pip compile`` runner, so
nothing here reaches the package index. The real resolutions against PyPI
run in ``make check-jsonnet-markers`` (CI's ``lint-python`` job), the check's
one network entry point.
"""

from pathlib import Path

import pytest

from scripts import check_jsonnet_markers as cjm
from scripts.check_docs_wheel_matrix import supported_minors
from scripts.check_jsonnet_markers import Compiled
from tests._fixtures.paths import CHECKOUT_ROOT

MINORS = ["3.10", "3.11"]
NEXT = "3.15"
_PYPROJECT = Path("/checkout/pyproject.toml")


def _failure(binding: str) -> Compiled:
    return Compiled(
        returncode=1,
        stdout="",
        stderr=(
            "error: No solution found when resolving dependencies\n"
            f"  cause: Because {binding}{{platform_machine == 'x86_64'}}==0.22.0 has no "
            "usable wheels ...\n"
            f"hint: Wheels are required for `{binding}` because building from source is disabled"
        ),
    )


def _resolved(binding: "str | None") -> Compiled:
    pins = "pydantic==2.13.4\npyyaml==6.0.3\n"
    if binding is not None:
        pins = f"{binding}==0.22.0\n" + pins
    return Compiled(returncode=0, stdout=pins, stderr="")


class _World:
    """A fake runner that answers every pinned case as the tables say, bar *overrides*."""

    def __init__(self, overrides: "dict[tuple[str, str], Compiled] | None" = None) -> None:
        self.answers = {
            (case.target, case.minor): (
                _failure(case.binding) if case.fails and case.binding else _resolved(case.binding)
            )
            for case in cjm.cases(MINORS, NEXT)
        }
        self.answers.update(overrides or {})
        self.argvs: list[list[str]] = []

    def __call__(self, argv: list[str]) -> Compiled:
        self.argvs.append(argv)
        target = argv[argv.index("--python-platform") + 1]
        minor = argv[argv.index("--python-version") + 1]
        return self.answers[(target, minor)]


def test_resolve_asks_for_a_binary_only_compile_of_the_project() -> None:
    world = _World()
    cjm.resolve("x86_64-manylinux_2_28", "3.10", pyproject=_PYPROJECT, run=world)
    [argv] = world.argvs
    assert argv[:3] == ["pip", "compile", str(_PYPROJECT)]
    assert argv[argv.index("--only-binary") + 1] == ":all:"
    assert argv[argv.index("--python-platform") + 1] == "x86_64-manylinux_2_28"
    assert argv[argv.index("--python-version") + 1] == "3.10"


def test_resolve_returns_the_binding_the_resolution_holds() -> None:
    def run(_argv: list[str]) -> Compiled:
        return _resolved("gojsonnet")

    assert cjm.resolve("aarch64-manylinux2014", "3.12", run=run) == "gojsonnet"


def test_resolve_returns_none_when_neither_binding_is_installed() -> None:
    def run(_argv: list[str]) -> Compiled:
        return _resolved(None)

    assert cjm.resolve("aarch64-manylinux2014", NEXT, run=run) is None


def test_resolve_raises_with_uvs_explanation_when_the_binary_only_resolve_fails() -> None:
    def run(_argv: list[str]) -> Compiled:
        return _failure("gojsonnet")

    with pytest.raises(cjm.ResolveFailedError) as raised:
        cjm.resolve("aarch64-unknown-linux-musl", "3.10", run=run)

    assert raised.value.target == "aarch64-unknown-linux-musl"
    assert raised.value.minor == "3.10"
    assert cjm.names_only(raised.value.stderr, "gojsonnet")
    assert not cjm.names_only(raised.value.stderr, "jsonnet")


def test_resolve_refuses_a_resolution_holding_both_bindings() -> None:
    def run(_argv: list[str]) -> Compiled:
        return Compiled(returncode=0, stdout="gojsonnet==0.22.0\njsonnet==0.22.0\n", stderr="")

    with pytest.raises(ValueError, match="both bindings"):
        cjm.resolve("x86_64-manylinux_2_28", "3.10", run=run)


def test_the_cases_cover_every_table_entry() -> None:
    expected = len(cjm.EXPECTED) * len(MINORS) + len(cjm.EXPECTED_315)
    expected += len(cjm.NO_WHEEL) * len(MINORS)
    all_cases = cjm.cases(MINORS, NEXT)
    assert len(all_cases) == expected
    assert {c.target for c in all_cases if c.minor == NEXT} == set(cjm.EXPECTED_315)
    assert {(c.target, c.binding) for c in all_cases if c.fails} == set(cjm.NO_WHEEL.items())


def test_the_audit_is_clean_when_every_case_holds() -> None:
    world = _World()
    assert cjm.audit(MINORS, NEXT, run=world) == []
    assert len(world.argvs) == len(cjm.cases(MINORS, NEXT))


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        pytest.param(
            {("x86_64-manylinux_2_28", "3.11"): _resolved("gojsonnet")},
            "expected jsonnet, resolved gojsonnet",
            id="wrong-binding",
        ),
        pytest.param(
            {("aarch64-manylinux2014", NEXT): _resolved("gojsonnet")},
            "expected no binding, resolved gojsonnet",
            id="binding-where-none-is-expected",
        ),
        pytest.param(
            {("aarch64-apple-darwin", "3.10"): _failure("jsonnet")},
            "expected jsonnet, but no binary-only resolution",
            id="expected-binding-fails",
        ),
        pytest.param(
            {("x86_64-manylinux2014", "3.10"): _resolved("jsonnet")},
            "expected the binary-only resolve to fail naming jsonnet",
            id="gap-resolves",
        ),
        pytest.param(
            {("aarch64-unknown-linux-musl", "3.11"): _failure("jsonnet")},
            "without naming gojsonnet alone",
            id="gap-fails-naming-the-other-binding",
        ),
    ],
)
def test_the_audit_names_each_contradiction(
    override: "dict[tuple[str, str], Compiled]", fragment: str
) -> None:
    problems = cjm.audit(MINORS, NEXT, run=_World(override))
    assert len(problems) == 1, problems
    assert fragment in problems[0]


def test_main_takes_no_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert cjm.main(["x86_64-manylinux_2_28"]) == 2
    assert "python -m scripts.check_jsonnet_markers" in capsys.readouterr().err


def test_the_canary_python_is_read_from_nightlys_python_canary_job() -> None:
    nightly = (CHECKOUT_ROOT / ".github" / "workflows" / "nightly.yml").read_text()
    canary = cjm.canary_python(nightly)
    supported = [
        f"{major}.{minor}"
        for major, minor in supported_minors((CHECKOUT_ROOT / "pyproject.toml").read_text())
    ]
    assert canary not in supported, "the canary is one past the supported minors"
    major, minor = (int(part) for part in canary.split("."))
    assert f"{major}.{minor - 1}" == supported[-1]


@pytest.mark.parametrize(
    ("value", "accepted"),
    [('"3.16"', True), ("3.16", False), ('"3"', False), ('"next"', False)],
)
def test_the_canary_python_must_be_a_quoted_minor(value: str, *, accepted: bool) -> None:
    nightly = f"jobs:\n  python-canary:\n    env:\n      CANARY_PYTHON: {value}\n"
    if accepted:
        assert cjm.canary_python(nightly) == "3.16"
    else:
        with pytest.raises(ValueError, match=r"quoted MAJOR\.MINOR"):
            cjm.canary_python(nightly)
