"""THE SPLIT-BRAIN GUARD for the coverage verbs: each leaf hands its library
function exactly the parsed flags and reports exactly the library's refusal in
flag spelling. No rule may live in a leaf.

Verified red when written:

- Making ``get`` pass ``clean=False`` regardless of ``--clean`` (hardcoding
  the keyword argument in its call to ``get_coverage``) failed only
  ``test_each_leaf_hands_the_library_exactly_its_flags[get -o /o --tier
  manual --ticket J-1 --note n --tester-name Al --tester-email a@x
  --clean]`` on ``assert fake.await_args.kwargs == kwargs`` — ``{...,
  'clean': False} != {..., 'clean': True}``; every other row (whose sample
  already passes ``--clean``-less/``False``) stayed green.
- Deleting the ``except CoverageInputError`` arm in ``get`` failed all three
  ``test_each_library_refusal_reaches_the_user_in_flag_spelling`` rows on
  ``assert result.exit_code == 2`` (``assert 1 == 2``,
  ``<Result SystemExit(1)>``) — ``CoverageInputError`` is itself a
  ``ValueError`` (``class CoverageInputError(FieldError, ValueError)``), so
  removing its own arm does not leave it uncaught: it falls into ``get``'s
  pre-existing ``except (ValueError, RuntimeError)`` arm, which exits 1
  instead of 2.
- Hardcoding ``overwrite=False`` in ``report``'s call to
  ``run_coverage_report`` (regardless of ``--overwrite-dir``) failed only
  ``test_each_leaf_hands_the_library_exactly_its_flags[report /o/run --dir
  /o/out --tier a=x.info --tier system --project-name P --prefix X
  --overwrite-dir]`` on ``assert fake.await_args.kwargs == kwargs`` —
  ``{'overwrite': False} != {'overwrite': True}``.
- Narrowing ``report``'s ``except (DestinationError, CoverageInputError)``
  arm to ``except (DestinationError,)`` failed both
  ``test_report_names_output_dirs_on_a_missing_run_dir`` and
  ``test_report_names_tier_on_a_duplicate_tier_name`` on ``assert
  result.exit_code == 2`` (``assert 1 == 2``, ``<Result SystemExit(1)>``) —
  the same ``CoverageInputError``-is-a-``ValueError`` mechanism as above:
  both fall into ``report``'s own ``except ValueError`` arm instead.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.cli.cov import cov_app
from otto.coverage.errors import CoverageInputError
from otto.coverage.report_inputs import ReportInputs
from otto.coverage.reports import CleanReport, GetReport
from tests._fixtures.dispatch import DispatchRunner
from tests.unit.cli.conftest import _flat

_REPORT = GetReport(
    cov_dir=Path("/o/cov"), tier="system", captures=[Path("c")], manual_captures=[], clean=None
)
_CLEAN = CleanReport(hosts={})
_STORE = MagicMock()
_STORE.file_count.return_value = 1
_STORE.overall_pct.return_value = 50.0

# (argv, seam, expected positional args, expected kwargs, an ok return)
ROWS = [
    (
        ["get", "--output", "/o"],
        "otto.coverage.get.get_coverage",
        (Path("/o"),),
        {
            "tier": None,
            "ticket": None,
            "note": None,
            "tester_name": None,
            "tester_email": None,
            "clean": False,
        },
        _REPORT,
    ),
    (
        [
            "get",
            "-o",
            "/o",
            "--tier",
            "manual",
            "--ticket",
            "J-1",
            "--note",
            "n",
            "--tester-name",
            "Al",
            "--tester-email",
            "a@x",
            "--clean",
        ],
        "otto.coverage.get.get_coverage",
        (Path("/o"),),
        {
            "tier": "manual",
            "ticket": "J-1",
            "note": "n",
            "tester_name": "Al",
            "tester_email": "a@x",
            "clean": True,
        },
        _REPORT,
    ),
    (["clean"], "otto.coverage.collect.clean_coverage", (), {}, _CLEAN),
    (
        [
            "report",
            "/o/run",
            "--dir",
            "/o/out",
            "--tier",
            "a=x.info",
            "--tier",
            "system",
            "--project-name",
            "P",
            "--prefix",
            "X",
            "--overwrite-dir",
        ],
        "otto.coverage.reporter.run_coverage_report",
        ([Path("/o/run/cov")], Path("/o/out").resolve(), ReportInputs()),
        {
            "project_name": "P",
            "tier_specs": [("a", Path("x.info")), ("system", None)],
            "prefix": Path("X"),
            "overwrite": True,
        },
        _STORE,
    ),
]


@pytest.mark.parametrize(
    ("argv", "seam", "args", "kwargs", "ret"), ROWS, ids=[" ".join(r[0]) for r in ROWS]
)
def test_each_leaf_hands_the_library_exactly_its_flags(argv, seam, args, kwargs, ret):
    fake = AsyncMock(return_value=ret)
    with patch(seam, fake):
        result = DispatchRunner().invoke(cov_app, argv, spec_name="cov")
    assert result.exit_code == 0, result.output
    fake.assert_awaited_once()
    assert fake.await_args.args == args
    assert fake.await_args.kwargs == kwargs


REFUSALS = [
    (
        ["get", "-o", "/o", "--tier", "ticket"],
        "otto.coverage.get.get_coverage",
        CoverageInputError("unknown tier 'ticket'; configured tiers: system", field="tier"),
        "--tier",
    ),
    (
        ["get", "-o", "/o", "--tier", "manual"],
        "otto.coverage.get.get_coverage",
        CoverageInputError(
            "tier 'manual' is a manual-kind tier and requires a ticket", field="ticket"
        ),
        "--ticket",
    ),
    (
        ["get"],
        "otto.coverage.get.get_coverage",
        CoverageInputError(
            "no output directory was given and none is set for this invocation",
            field="output_dir",
        ),
        "--output",
    ),
]


@pytest.mark.parametrize(
    ("argv", "seam", "exc", "flag"), REFUSALS, ids=[" ".join(r[0]) for r in REFUSALS]
)
def test_each_library_refusal_reaches_the_user_in_flag_spelling(argv, seam, exc, flag):
    with patch(seam, AsyncMock(side_effect=exc)):
        result = DispatchRunner().invoke(cov_app, argv, spec_name="cov")
    assert result.exit_code == 2, result.output
    assert f"Invalid value for {flag}" in _flat(result.output)
    assert _flat(str(exc)) in _flat(result.output)


# These two rows drive the REAL ``run_coverage_report`` (no patch, unlike the
# row above): its refusals are the library's, by field, and must reach the
# user byte-identical in ``report``'s own flag spelling (``_REPORT_FLAGS``),
# never rewritten.


def test_report_names_output_dirs_on_a_missing_run_dir(tmp_path):
    missing = tmp_path / "no_such_run"
    with patch("otto.coverage.report_inputs.resolve_report_inputs", return_value=ReportInputs()):
        result = DispatchRunner().invoke(
            cov_app,
            ["report", str(missing), "--dir", str(tmp_path / "out")],
            spec_name="cov",
        )
    assert result.exit_code == 2, result.output
    out = _flat(result.output)
    assert "Invalid value for OUTPUT_DIRS" in out
    assert "output directory does not exist" in out


def test_report_names_tier_on_a_duplicate_tier_name(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "cov").mkdir(parents=True)
    result = DispatchRunner().invoke(
        cov_app,
        [
            "report",
            str(run_dir),
            "--tier",
            "a=x.info",
            "--tier",
            "a=y.info",
            "--dir",
            str(tmp_path / "out"),
        ],
        spec_name="cov",
    )
    assert result.exit_code == 2, result.output
    out = _flat(result.output)
    assert "Invalid value for --tier" in out
    assert "Duplicate tier name: 'a'" in out
