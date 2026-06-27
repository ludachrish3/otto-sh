import logging

from otto.logger import get_otto_logger


def test_otto_logger_is_a_plain_standard_logger():
    """Regression guard for the duplicate-import hazard: with a custom Logger
    subclass the singleton's class could diverge across a double-import. A plain
    logging.Logger has a stable identity, so this can never recur."""
    lg = get_otto_logger()
    assert type(lg) is logging.Logger
    assert lg is logging.getLogger('otto')
    assert lg.name == 'otto'


def test_get_otto_logger_named_is_child_under_otto():
    child = get_otto_logger('host')
    assert child is logging.getLogger('otto.host')
    assert child.name == 'otto.host'


def test_no_ottologger_symbol_exported():
    import otto.logger as pkg
    assert not hasattr(pkg, 'OttoLogger')
