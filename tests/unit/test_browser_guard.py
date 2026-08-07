"""Pins for the browser suites' configure-time "is this gate armed?" predicate.

``browser_tests_could_run()`` decides whether a missing/stale web build should
``pytest.exit`` the whole session. It is a *run* precondition — the dist is
what the browser tests load — so it must stay armed for any session that could
execute a browser item, and stay quiet for any session that could not.

The ``--collect-only`` leg is issue #196: a session that only enumerates runs
nothing, so the precondition cannot be violated, but the gate fired anyway and
aborted collection with rc=2 for every consumer that enumerates the tree
without a build (all six hostless CI lanes, the unit-repeat lane, and any
fresh checkout — ``src/otto/_webassets/*/`` is gitignored).

These drive a REAL parsed ``pytest.Config`` rather than a stub with the two
attributes hand-written on it: a stub spells the option names itself, so it
agrees with itself while the predicate reads something pytest never sets —
the failure mode that would leave the gate firing in exactly the lanes this
pins. Parsing real argv is what makes ``collectonly`` a checked name.

The ``-c`` in ``make_config`` is load-bearing, not tidiness. Parsing argv
inside the repo root makes pytest apply this project's ``addopts``, which
carry ``--cov=otto --cov-context=test -n auto`` — so a bare ``get_config()``
here starts a SECOND coverage session inside the xdist worker running this
test, which corrupts that worker's data file (``no such table: context``) and
takes the controller down with it. Observed: 300 errors and an xdist
INTERNALERROR across the hostless gate, from six lines of test setup. An
inert ini keeps the parse real and the side effects nil.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from _pytest.config import get_config

from tests._fixtures._browser_guard import BROWSER_TEST_MARKERS, browser_tests_could_run

ConfigFactory = Callable[..., pytest.Config]


@pytest.fixture(scope="module")
def make_config(tmp_path_factory: pytest.TempPathFactory) -> ConfigFactory:
    """Build a real ``pytest.Config`` that carries none of this repo's addopts."""
    ini: Path = tmp_path_factory.mktemp("browser_guard") / "pytest.ini"
    ini.write_text("[pytest]\n")

    def _make(*argv: str) -> pytest.Config:
        args = ["-c", str(ini), *argv]
        config = get_config(args)
        config.parse(args)
        # The isolation above is invisible when it works and catastrophic when
        # it lapses, so assert it rather than trusting the `-c`: if a future
        # pytest or plugin starts honouring the project's addopts through it
        # anyway, this fails here instead of as unattributable coverage-DB
        # corruption in whichever worker happened to run this module.
        assert not getattr(config.option, "cov_source", None), (
            "the throwaway ini leaked this repo's --cov addopts into a parsed "
            "config — it would start a second coverage session in this worker"
        )
        assert getattr(config.option, "numprocesses", None) is None, (
            "the throwaway ini leaked this repo's -n auto into a parsed config"
        )
        return config

    return _make


class TestTheGateStaysArmed:
    """Sessions that could execute a browser item must still exit loudly."""

    def test_an_unfiltered_run_arms_the_gate(self, make_config: ConfigFactory):
        assert browser_tests_could_run(make_config()) is True

    def test_a_run_selecting_browser_arms_the_gate(self, make_config: ConfigFactory):
        assert browser_tests_could_run(make_config("-m", "browser")) is True

    def test_a_positive_hostless_expression_arms_the_gate(self, make_config: ConfigFactory):
        # Every browser test also carries `hostless`, so `-m hostless` picks
        # them up. Keying only off `browser` would read this as deselected and
        # let the run reach N missing-dist fixture errors — the noise the gate
        # replaces. Pins the BROWSER_TEST_MARKERS rationale, not just the name.
        assert "hostless" in BROWSER_TEST_MARKERS
        assert browser_tests_could_run(make_config("-m", "hostless")) is True


class TestTheGateStaysQuiet:
    """Sessions that cannot execute a browser item must not veto the session."""

    def test_a_deselecting_expression_disarms_the_gate(self, make_config: ConfigFactory):
        assert browser_tests_could_run(make_config("-m", "not browser")) is False

    def test_collect_only_disarms_the_gate(self, make_config: ConfigFactory):
        # Issue #196. --collect-only imports every conftest but runs no test,
        # so the dist precondition is not yet in play; exiting here turns a
        # pure enumeration into a session abort.
        assert browser_tests_could_run(make_config("--collect-only")) is False

    def test_collect_only_disarms_even_when_the_marks_select_browser(
        self, make_config: ConfigFactory
    ):
        # The two questions are independent: "could -m keep a browser item"
        # says yes here, and the answer still has to be "nothing will run".
        assert browser_tests_could_run(make_config("--collect-only", "-m", "browser")) is False
