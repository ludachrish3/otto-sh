"""A workflow job that installs a Python must actually RUN on it.

uv picks a project's interpreter from ``--python``, then ``UV_PYTHON``, then
the version file -- here ``.python-versions``, whose FIRST line (the floor)
is the one a plain ``uv sync`` / ``uv run`` asks for. Worse, ``uv run`` does
not just prefer it: it REBUILDS a ``.venv`` created on any other interpreter.
So a job can ``uv python install 3.13`` and ``uv sync --python 3.13``, and
the next ``uv run pytest`` -- or a ``make`` target that spells one -- quietly
tests the floor under a 3.13 label. Two jobs shipped exactly that: nightly's
``stability-matrix`` soaked 3.10 in every cell from the day it was added, and
the next-Python canary "climbed" to rung 10 on 2026-09-16 with most rungs on
3.10 (#334).

nox is the exception: the uv backend builds each session's venv on the
session's own interpreter, whatever the launcher's ``.venv`` runs. A job
whose only ``uv run`` is ``uv run nox`` is therefore safe without the knob.

The rule: a job that installs any interpreter other than the floor either
sets ``UV_PYTHON`` to it (job ``env``, or an ``export`` in a run step) or
reaches ``uv run`` only through nox.
"""

import re
import shlex

import pytest
import yaml

from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"

# `uv run` options that take a value, so the value is not mistaken for the
# command being run. Only the ones the workflows use need to be here: an
# unknown one would make the command look like its value, which is never
# `nox`, so the job is flagged -- loud rather than green.
_VALUED_OPTIONS = {"--group", "--python", "--with", "--extra"}


def _floor() -> str:
    return (PROJECT_ROOT / ".python-versions").read_text().split()[0]


def _unquote(word: str) -> str:
    return word.strip("\"'")


def _run_scripts(job: dict) -> "list[str]":
    return [step["run"] for step in job.get("steps", []) if "run" in step]


def _installed_pythons(scripts: "list[str]") -> "list[str]":
    pattern = re.compile(r"uv python install\s+(.+?)\s*$", re.MULTILINE)
    return [_unquote(m) for s in scripts for m in pattern.findall(s)]


def _runs_outside_nox(scripts: "list[str]") -> bool:
    """True when a script reaches a plain `uv run` other than `uv run nox`, or a `make` target."""
    for script in scripts:
        for line in script.splitlines():
            code = line.split("#", 1)[0]
            if re.search(r"(^|[\s;&|(])make\s", code):
                return True
            for match in re.finditer(r"\buv run\b(.*)", code):
                words = shlex.split(match.group(1), comments=True)
                command = None
                skip = False
                for word in words:
                    if skip:
                        skip = False
                    elif word in _VALUED_OPTIONS:
                        skip = True
                    elif not word.startswith("-"):
                        command = word
                        break
                if command != "nox":
                    return True
    return False


def _sets_uv_python(job: dict, scripts: "list[str]", python: str) -> bool:
    if _unquote(str(job.get("env", {}).get("UV_PYTHON", ""))) == python:
        return True
    exports = (_unquote(m) for s in scripts for m in re.findall(r"export UV_PYTHON=(\S+)", s))
    return python in exports


def _offenders(workflows: "dict[str, dict]", floor: str) -> "list[str]":
    offenders = []
    for filename, workflow in workflows.items():
        for name, job in (workflow.get("jobs") or {}).items():
            scripts = _run_scripts(job)
            if not _runs_outside_nox(scripts):
                continue
            offenders.extend(
                f"{filename}::{name} installs {python}"
                for python in _installed_pythons(scripts)
                if python != floor and not _sets_uv_python(job, scripts, python)
            )
    return offenders


def test_every_job_runs_on_the_python_it_installs() -> None:
    workflows = {p.name: yaml.safe_load(p.read_text()) for p in sorted(WORKFLOWS.glob("*.yml"))}
    offenders = _offenders(workflows, _floor())
    assert not offenders, (
        "these jobs install a non-floor Python but run plain `uv run`/`make` without "
        f"UV_PYTHON, so uv rebuilds .venv on {_floor()} from .python-versions: {offenders}"
    )


def _job(*runs: str, **env: str) -> dict:
    job: dict = {"steps": [{"run": run} for run in runs]}
    if env:
        job["env"] = env
    return job


_MATRIX = "${{ matrix.v }}"


@pytest.mark.parametrize(
    ("job", "flagged"),
    [
        pytest.param(
            _job(f"uv python install {_MATRIX}", "make stability-unit"),
            True,
            id="make-without-knob",
        ),
        pytest.param(
            _job('uv python install "$PY"', "uv run --frozen --group dev pytest"),
            True,
            id="plain-uv-run-without-knob",
        ),
        pytest.param(
            _job('uv python install "$PY"', 'export UV_PYTHON="$PY"\nuv run pytest'),
            False,
            id="exported-knob",
        ),
        pytest.param(
            _job(f"uv python install {_MATRIX}", "make stability-unit", UV_PYTHON=_MATRIX),
            False,
            id="job-env-knob",
        ),
        pytest.param(
            _job(f"uv python install {_MATRIX}", f"uv run nox -s t-{_MATRIX}"),
            False,
            id="nox-only",
        ),
        pytest.param(
            _job("uv python install 3.10", "make coverage"),
            False,
            id="floor-only",
        ),
    ],
)
def test_the_rule_flags_a_job_without_the_knob(job: dict, flagged: bool) -> None:
    """The guard's red is real: each shape is judged the way the rule says."""
    assert bool(_offenders({"wf.yml": {"jobs": {"j": job}}}, "3.10")) is flagged
