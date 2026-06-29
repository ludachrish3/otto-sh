from otto.logger.mode import LogMode, effective_mode


def test_logmode_values():
    assert LogMode.NORMAL.value == "normal"
    assert LogMode.QUIET.value == "quiet"
    assert LogMode.NEVER.value == "never"


def test_logmode_rank_orders_normal_quiet_never():
    assert LogMode.NORMAL.rank < LogMode.QUIET.rank < LogMode.NEVER.rank


def test_effective_mode_is_most_restrictive():
    assert effective_mode(LogMode.NORMAL, LogMode.QUIET) is LogMode.QUIET
    assert effective_mode(LogMode.QUIET, LogMode.NEVER) is LogMode.NEVER
    assert effective_mode(LogMode.NORMAL, LogMode.NORMAL) is LogMode.NORMAL


def test_effective_mode_no_args_is_normal():
    assert effective_mode() is LogMode.NORMAL
