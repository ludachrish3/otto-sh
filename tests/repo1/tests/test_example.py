import pytest


def test_example():
    """Minimal test confirming repo1 tests are collected by otto."""
    assert True

@pytest.mark.integration
def test_example_integration():
    """Minimal test confirming repo1 tests are collected by otto."""
    assert True
