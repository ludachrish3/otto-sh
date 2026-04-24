"""Root test conftest — registers the shared async timeout fixture for dev test runs."""

from otto.suite import timeout


def pytest_configure(config):  # type: ignore[no-untyped-def]
    if not config.pluginmanager.has_plugin('otto-timeout'):
        config.pluginmanager.register(timeout, name='otto-timeout')
