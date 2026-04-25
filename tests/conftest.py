"""Root test conftest — registers the shared async timeout fixture for dev test runs."""

import os

# Disable colored CLI output before typer/click/rich are imported anywhere.
# CI runners (e.g. GitHub Actions) set FORCE_COLOR, which causes Rich to embed
# ANSI escapes in help/error text and breaks substring assertions like
# `'--flag' in result.output`.
os.environ["NO_COLOR"] = "1"
os.environ["TERM"] = "dumb"
for _var in ("FORCE_COLOR", "CLICOLOR_FORCE", "PY_COLORS", "CLICOLOR"):
    os.environ.pop(_var, None)

from otto.suite import timeout  # noqa: E402


def pytest_configure(config):  # type: ignore[no-untyped-def]
    if not config.pluginmanager.has_plugin('otto-timeout'):
        config.pluginmanager.register(timeout, name='otto-timeout')
