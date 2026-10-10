"""Providers are subscriptions: order kept, repo captured, test load refused."""

import pytest

from otto.host.dev_tool import (
    DEV_TOOL_PROVIDERS,
    register_dev_tool_provider,
    registered_dev_tool_providers,
)
from otto.host.product import (
    PRODUCT_PROVIDERS,
    register_product_provider,
    registered_product_providers,
)
from otto.registry import RegistrationRefused, loading_test_files, registering_repo

from .. import conformance

COVERS = ["otto.host.product:PRODUCT_PROVIDERS", "otto.host.dev_tool:DEV_TOOL_PROVIDERS"]


@pytest.mark.parametrize(
    ("table", "wrapper", "reader"),
    [
        (PRODUCT_PROVIDERS, register_product_provider, registered_product_providers),
        (DEV_TOOL_PROVIDERS, register_dev_tool_provider, registered_dev_tool_providers),
    ],
)
def test_the_wrapper_keeps_order_repo_and_attribution(table, wrapper, reader):
    def a(host): ...

    def b(host): ...

    with registering_repo("acme"):
        wrapper(a)
    wrapper(b)
    assert reader()[-2:] == [(a, "acme"), (b, None)]
    assert [s.origin for s in table.items()[-2:]] == [__name__, __name__]
    with loading_test_files(), pytest.raises(RegistrationRefused):
        wrapper(a)


@pytest.mark.parametrize("table", [PRODUCT_PROVIDERS, DEV_TOOL_PROVIDERS])
def test_subscription_conformance(table):
    def first(host): ...

    def second(host): ...

    conformance.assert_subscription(table, value_a=first, value_b=second)
