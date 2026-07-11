"""Import-hygiene guarantees for otto's logging package.

``get_logger()`` is deleted (clean break): otto modules and library
consumers alike use the stdlib idiom directly (``logging.getLogger(__name__)``
internally, ``logging.getLogger("otto")`` — or any name — for consumers).
These tests pin the payoff: a bare ``import otto`` (or ``import otto.logger``)
must stay cheap, and the library-citizen ``NullHandler`` must attach exactly
once regardless of which entry point runs first.

Subprocess tests mirror Task 17's `test_reservations_import_is_typer_free`
pattern (``sys.modules`` is process-global, so a prior test in the same
process could mask a regression).
"""

import subprocess
import sys

import pytest


def test_import_otto_logger_does_not_pull_in_rich():
    """``import otto.logger`` alone must not import ``rich``.

    ``rich`` is only needed by ``otto.logger.management`` (CLI-side handler
    config); the package itself must stay stdlib-only until ``management``
    is actually touched.
    """
    code = "import sys, otto.logger; sys.exit(1 if 'rich' in sys.modules else 0)"
    result = subprocess.run([sys.executable, "-c", code], check=False)
    assert result.returncode == 0


def test_import_otto_attaches_exactly_one_nullhandler():
    """A bare ``import otto`` attaches exactly one ``NullHandler`` to ``'otto'``.

    Regression guard: the NullHandler used to live in ``otto.logger``'s
    eager ``__init__``, so a bare ``import otto`` (which never touched
    ``otto.logger``) attached zero handlers — importing otto as a library
    was silent only by accident of never having emitted anything yet, not
    because logging was actually configured to be silent.
    """
    code = (
        "import sys, logging, otto\n"
        "handlers = logging.getLogger('otto').handlers\n"
        "n = sum(1 for h in handlers if isinstance(h, logging.NullHandler))\n"
        "sys.exit(0 if n == 1 else 1)"
    )
    result = subprocess.run([sys.executable, "-c", code], check=False)
    assert result.returncode == 0


def test_management_is_still_reachable_via_lazy_getattr():
    """``from otto.logger import management`` works (PEP 562 lazy re-export).

    Accessing the attribute is what triggers the (now-deferred) rich-heavy
    import — proven here in-process: absent before touching ``management``,
    present after.
    """
    code = (
        "import sys, otto.logger\n"
        "assert 'rich' not in sys.modules, 'rich imported before management was touched'\n"
        "from otto.logger import management\n"
        "assert 'rich' in sys.modules, 'accessing management should import rich'\n"
        "assert management.__name__ == 'otto.logger.management'\n"
        "sys.exit(0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], check=False)
    assert result.returncode == 0, result.stderr


def test_management_attribute_is_cached_module_identity():
    import otto.logger

    first = otto.logger.management
    second = otto.logger.management
    assert first is second


def test_unknown_attribute_still_raises():
    import otto.logger

    with pytest.raises(AttributeError, match=r"otto\.logger"):
        otto.logger.definitely_not_a_real_attribute  # noqa: B018 — deliberate AttributeError probe


def test_no_ottologger_symbol_exported():
    import otto.logger as pkg

    assert not hasattr(pkg, "OttoLogger")
