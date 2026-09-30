"""The CLI spells a library error's field names as flags at one site."""

from pathlib import Path

from otto.cli.invoke import spell_flags, usage_error_from
from otto.coverage.config import DestinationError
from otto.params import OptionsValidationError

FLAGS = {
    "cov=False": "--no-cov",
    "random_order=False": "--no-random",
    "cov": "--cov",
    "random_order": "--random",
    "cov_report_dir": "--cov-report-dir",
    "overwrite_cov_report_dir": "--overwrite-cov-report-dir",
    "cov_report": "--cov-report",
    "cov_dir": "--cov-dir",
    "overwrite_cov_dir": "--overwrite-cov-dir",
    "cov_tickets_json": "--cov-tickets-json",
    "seed": "--seed",
}


def test_remedy_phrase_becomes_pass_flag():
    text = "cov_report_dir target /x is not empty; set overwrite_cov_report_dir=True to clear it."
    assert (
        spell_flags(text, FLAGS)
        == "--cov-report-dir target /x is not empty; pass --overwrite-cov-report-dir to clear it."
    )


def test_literal_value_keys_win_over_bare_fields():
    text = (
        "cov=False cannot be combined with cov_dir, cov_report, cov_report_dir or cov_tickets_json"
    )
    assert spell_flags(text, FLAGS) == (
        "--no-cov cannot be combined with --cov-dir, --cov-report, --cov-report-dir or "
        "--cov-tickets-json"
    )


def test_bare_fields_match_whole_words_only():
    assert (
        spell_flags("cov_report_dir and cov_report", FLAGS) == "--cov-report-dir and --cov-report"
    )


def test_bare_fields_never_match_inside_a_spelled_flag():
    assert spell_flags("cov=False cannot be combined with cov_dir", FLAGS) == (
        "--no-cov cannot be combined with --cov-dir"
    )
    assert spell_flags("seed requires random_order", FLAGS) == "--seed requires --random"


def test_usage_error_carries_the_spelled_message_and_the_param_hint():
    # The message is deliberately NOT led by the field's own spelling (every
    # real DestinationError message is: "cov_dir target ..."): this test's
    # subject is the param_hint alone, which
    # test_usage_error_drops_a_hint_the_message_already_leads_with covers.
    err = OptionsValidationError(
        "the destination is not empty; set overwrite_cov_dir=True to clear cov_dir's target."
    )
    err.field = "cov_dir"
    bad = usage_error_from(err, flags=FLAGS)
    assert "pass --overwrite-cov-dir" in str(bad.message)
    assert bad.param_hint == "--cov-dir"


def test_usage_error_drops_a_hint_the_message_already_leads_with():
    # Every real DestinationError message leads with its field
    # ("cov_dir target ..."), which spells to "--cov-dir target ...": a
    # param_hint naming that same flag would only make click repeat it
    # ("Invalid value for --cov-dir: --cov-dir target ..."), so it is
    # dropped rather than kept.
    err = DestinationError(
        Path("/x"), field="cov_dir", remedy_field="overwrite_cov_dir", kind="not_empty"
    )
    bad = usage_error_from(err, flags=FLAGS)
    assert str(bad.message).startswith("--cov-dir target")
    assert bad.param_hint is None


def test_usage_error_without_flags_passes_the_message_through():
    err = OptionsValidationError("count: Field required")
    assert str(usage_error_from(err).message) == "count: Field required"


def test_usage_error_without_flags_has_no_param_hint():
    err = OptionsValidationError("count: Field required")
    assert usage_error_from(err).param_hint is None


def test_usage_error_hint_is_none_when_the_field_is_not_in_the_map():
    err = DestinationError(
        Path("/x"), field="output_dir", remedy_field="overwrite_output_dir", kind="not_empty"
    )
    assert usage_error_from(err, flags=FLAGS).param_hint is None


def test_usage_error_keeps_the_destination_path_out_of_the_spelling():
    # A path that happens to contain a field name as one of its own segments
    # (`/tmp/cov`, `out/cov_report/x`) must survive byte-for-byte: it is the
    # user's own filesystem path, not a field spell_flags should ever touch.
    # `Path("cov")` and `Path("cov_dir")` are the sharper case: the path's
    # own text is a SUBSTRING of the field name being spelled, which used to
    # break text-rewriting entirely (the path-sentinel swap had no boundary,
    # so it clobbered the "cov" inside "cov_dir" too) — a DestinationError is
    # now rebuilt from its fields instead of rewritten, so this can't recur.
    cases = {
        Path("/tmp/cov"): (
            "--cov-dir target /tmp/cov is not empty; pass --overwrite-cov-dir to clear it."
        ),
        Path("out/cov_report/x"): (
            "--cov-dir target out/cov_report/x is not empty; pass --overwrite-cov-dir to clear it."
        ),
        Path("cov"): "--cov-dir target cov is not empty; pass --overwrite-cov-dir to clear it.",
        Path("cov_dir"): (
            "--cov-dir target cov_dir is not empty; pass --overwrite-cov-dir to clear it."
        ),
    }
    for path, expected in cases.items():
        err = DestinationError(
            path, field="cov_dir", remedy_field="overwrite_cov_dir", kind="not_empty"
        )
        message = str(usage_error_from(err, flags=FLAGS).message)
        assert message == expected


def test_usage_error_spells_a_not_writable_destination_message():
    err = DestinationError(
        Path("cov_dir"),
        field="cov_dir",
        remedy_field="overwrite_cov_dir",
        kind="not_writable",
        reason="Permission denied",
    )
    message = str(usage_error_from(err, flags=FLAGS).message)
    assert message == "--cov-dir target cov_dir cannot be written: Permission denied."


def test_usage_error_spells_a_not_a_directory_destination_message():
    err = DestinationError(
        Path("cov_dir"), field="cov_dir", remedy_field="overwrite_cov_dir", kind="not_a_directory"
    )
    message = str(usage_error_from(err, flags=FLAGS).message)
    assert message == "--cov-dir target cov_dir is not a directory."


def test_flag_value_with_a_backslash_is_inserted_literally():
    # `re.sub`'s string-replacement form reads `\1` as a backreference; a
    # flag spelling that happens to contain a backslash must not trip that.
    assert spell_flags("cov bad", {"cov": r"--c\1v"}) == r"--c\1v bad"
