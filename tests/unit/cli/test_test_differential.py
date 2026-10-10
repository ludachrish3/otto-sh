"""THE SPLIT-BRAIN GUARD for ``otto test``: the CLI hands ``run_tests`` exactly
``RunOptions(**parsed flags)`` and reports exactly the library's refusals, in
flag spelling. No rule may live in the CLI. Every run flag joins the table by
construction (``_run_params``), so a new flag is guarded the day it is added.

Scope: this proves the rule for every run flag ALONE, for the three
contradiction pairs in ``CONTRADICTIONS``, and for a bad ``--cov-report-dir``
destination. A CLI-side rule that only fires on a flag COMBINATION outside
``CONTRADICTIONS`` is not caught here.

Verified red when written: adding ``fields["cov_clean"] = False`` before the
constructor in ``_check_selection`` failed every flag row but ``cov_clean``'s
own (whose sample already picks ``False``) on
``assert handed == dataclasses.asdict(expected)``, each with the same diff:
``{'cov_clean': False} != {'cov_clean': True}``.
"""

import dataclasses
import inspect
import re
from pathlib import Path
from typing import Any, get_args, get_origin

import pytest

from otto.cli.invoke import spell_flags
from otto.cli.test import RUN_FLAGS, _run_params
from otto.coverage.config import DestinationError
from otto.params import OptionsValidationError
from otto.suite.run import RunOptions, prepare_run
from tests._fixtures.bootstrap_seam import patch_bootstrap
from tests.unit.cli.conftest import _flat, _repo_with_tickets_configured, _squashed

_SKIP = {"list_markers", "list_tests"}  # not RunOptions fields


def _real_type(annotation: object) -> object:
    """Unwrap ``Annotated[X, typer.Option(...)]`` and ``Optional[X]``/``X | None`` to ``X``."""
    inner = get_args(annotation)[0]  # every run flag is Annotated[X, ...]
    origin = get_origin(inner)
    if origin is not None:
        args = [a for a in get_args(inner) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return inner


def _sample(param: inspect.Parameter, tmp_path: Path):
    """One non-default value per flag type, and the argv that spells it."""
    flag = RUN_FLAGS[param.name]
    real_type = _real_type(param.annotation)
    if isinstance(real_type, type) and issubclass(real_type, Path):
        value = tmp_path / param.name
        return value, [flag, str(value)]
    if real_type is bool:
        if param.default is True:
            return False, [RUN_FLAGS.get(f"{param.name}=False", flag.replace("--", "--no-", 1))]
        return True, [flag]
    if real_type is int:
        return 3, [flag, "3"]
    if real_type is float:
        return 2.5, [flag, "2.5"]
    return "sample", [flag, "sample"]


def _rows():
    return [p for p in _run_params() if p.name not in _SKIP]


@pytest.mark.parametrize("param", _rows(), ids=lambda p: p.name)
def test_each_flag_reaches_run_tests_as_the_class_constructs_it(
    param, capture_cov, tmp_path, monkeypatch
):
    patch_bootstrap(monkeypatch, [_repo_with_tickets_configured()])
    value, argv = _sample(param, tmp_path)
    contradiction_message = None
    try:
        expected = RunOptions(**{param.name: value})
    except OptionsValidationError as exc:
        contradiction_message = str(exc)
    if contradiction_message is not None:
        # A flag that is, on its own, a contradiction gets the same proof as
        # CONTRADICTIONS: exit 2, nothing handed to run_tests, the library's
        # message in flag spelling. No flag is one today, but no row may
        # silently skip if a future one becomes one.
        exit_code, handed, output = capture_cov(argv)
        assert exit_code == 2
        assert handed == {}
        assert _squashed(spell_flags(contradiction_message, RUN_FLAGS)) in _squashed(output)
        return
    exit_code, handed, output = capture_cov(argv)
    assert exit_code == 0, output
    assert handed == dataclasses.asdict(expected)


CONTRADICTIONS = [
    # (fields, the library message's leading substring — required by this
    # repo's PT011 gate, which demands `match=` for any *ValidationError)
    ({"cov": False, "cov_dir": "{tmp}"}, "cov=False cannot be combined with"),
    ({"cov": False, "cov_report": True}, "cov=False cannot be combined with"),
    ({"seed": 7, "random_order": False}, "seed cannot be combined with random_order=False"),
    ({"cov_dir": "{tmp}", "cov_report_dir": "{tmp}"}, "cov_report_dir cannot be or contain"),
]


SINGLE_FIELD_REFUSALS = [
    ({"monitor_interval": 0.5}, "monitor_interval: interval must be at least"),
    ({"monitor_hosts": "("}, "monitor_hosts: host pattern"),
]


def _argv(fields: dict, tmp_path: Path) -> list[str]:
    out: list[str] = []
    for name, value in fields.items():
        if value is False:
            out.append(RUN_FLAGS[f"{name}=False"])
        elif value is True:
            out.append(RUN_FLAGS[name])
        else:
            out += [RUN_FLAGS[name], str(value).format(tmp=tmp_path)]
    return out


@pytest.mark.parametrize(
    ("fields", "message"), SINGLE_FIELD_REFUSALS, ids=["monitor_interval", "monitor_hosts"]
)
def test_each_single_field_refusal_is_the_librarys_message_in_flag_spelling(
    fields, message, capture_cov, tmp_path
):
    with pytest.raises(OptionsValidationError, match=re.escape(message)) as excinfo:
        RunOptions(**fields)
    exit_code, handed, output = capture_cov(_argv(fields, tmp_path))
    assert exit_code == 2
    assert handed == {}
    assert _squashed(spell_flags(str(excinfo.value), RUN_FLAGS)) in _squashed(output)


def _resolve(value: object, tmp_path: Path) -> object:
    """Turn a CONTRADICTIONS field value into RunOptions' own type for it.

    Only a ``{tmp}``-templated string names a destination and becomes a
    ``Path``; any other value (a plain ``bool``/``int``/str) is passed
    through as the type RunOptions itself declares for that field.
    """
    if isinstance(value, str) and "{tmp}" in value:
        return Path(value.format(tmp=tmp_path))
    return value


@pytest.mark.parametrize(
    ("fields", "message"), CONTRADICTIONS, ids=["+".join(fields) for fields, _ in CONTRADICTIONS]
)
def test_each_contradiction_is_the_librarys_message_in_flag_spelling(
    fields, message, capture_cov, tmp_path
):
    resolved: "dict[str, Any]" = {k: _resolve(v, tmp_path) for k, v in fields.items()}
    with pytest.raises(OptionsValidationError, match=message) as excinfo:
        RunOptions(**resolved)
    exit_code, handed, output = capture_cov(_argv(fields, tmp_path))
    assert exit_code == 2
    assert handed == {}
    # `_squashed` drops rich's panel borders and all whitespace on both sides:
    # click wraps a long spelled message across lines and hard-breaks a path
    # longer than the panel, so a plain substring check can false-negative on
    # where it wraps.
    assert _squashed(spell_flags(str(excinfo.value), RUN_FLAGS)) in _squashed(output)


def test_a_path_segment_named_like_a_field_is_never_spelled_as_a_flag(capture_cov, tmp_path):
    """The flag spelling rewrites field names word by word, so a message that
    quoted the user's ``<tmp>/cov`` would print ``<tmp>/--cov``. The refusal
    names the fields only; proven red by putting the paths back in it."""
    same = tmp_path / "cov"
    exit_code, handed, output = capture_cov(["--cov-dir", str(same), "--cov-report-dir", str(same)])
    assert exit_code == 2
    assert handed == {}
    assert "--cov-report-dir cannot be or contain --cov-dir" in _flat(output)
    assert "/--cov" not in _squashed(output)


def test_a_bad_destination_is_the_librarys_message_in_flag_spelling(capture_cov, tmp_path):
    (tmp_path / "stale.html").write_text("stale")
    with pytest.raises(DestinationError) as excinfo:
        prepare_run(RunOptions(cov_report_dir=tmp_path))
    exit_code, handed, output = capture_cov(["--cov-report-dir", str(tmp_path)])
    assert exit_code == 2
    assert handed == {}
    assert _squashed(spell_flags(str(excinfo.value), RUN_FLAGS)) in _squashed(output)
