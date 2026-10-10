"""The run's product variant: the run policy's, debug unless the root flag says field."""

import pytest

from otto import context, invocation


def test_the_default_variant_is_debug():
    assert context.variant() == "debug"


def test_set_variant_is_read_back_and_reset_by_its_token():
    token = context.set_variant("field")
    assert context.variant() == "field"
    context.reset_variant(token)
    assert context.variant() == "debug"


def test_set_variant_refuses_anything_but_the_two_names():
    with pytest.raises(ValueError, match=r"variant must be 'debug' or 'field', got 'release'"):
        context.set_variant("release")  # type: ignore[arg-type]


def test_variants_is_the_public_pair():
    assert invocation.VARIANTS == ("debug", "field")
